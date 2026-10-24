#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
# Adapted from vrt49 examples/hardware/zu11eg.py (LGPL-2.1-or-later): same adi-lg request shape,
# shared hardware lock, JUnit validation and environment guards.
"""Run the hopcap hardware suite on tron through adi-lg.

--inspect / --dry-run  print the exact pytest + adi-lg argv and the required environment (offline)
--run                  acquire tron, boot the staged artifacts, run tests/hardware, verify JUnit + evidence
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COORDINATOR = "10.0.0.41:20408"
LOCK = Path("/tmp/vrt49-corundum-hardware.lock")
NODES = (
    "tests/hardware/test_hw_hopcap.py::test_boot_and_identity",
    "tests/hardware/test_hw_hopcap.py::test_udp_path_board_to_host",
    "tests/hardware/test_hw_hopcap.py::test_hop_capture_plan",
    "tests/hardware/test_hw_hopcap.py::test_negative_no_edge_no_hop",
)
REQUIRED_ENV = (
    "HOPCAP_PLAN",
    "HOPCAP_OUT",
    "VRT49_CORUNDUM_BOOT",
    "VRT49_CORUNDUM_BOOT_BIN_URL",
    "VRT49_CORUNDUM_BOOT_BIN_SHA256",
    "VRT49_CORUNDUM_KERNEL_URL",
    "VRT49_CORUNDUM_KERNEL_SHA256",
    "VRT49_CORUNDUM_DTB_URL",
    "VRT49_CORUNDUM_DTB_SHA256",
)
OPTIONAL_ENV = (
    "HOPCAP_RX_INTERFACE",
    "HOPCAP_RX_CPU",
    "HOPCAP_BIND",
    "HOPCAP_MIN_COMPLETE_FRACTION",
    "HOPCAP_COMMIT",
)


def commands(junit: Path, wait: int) -> tuple[list[str], list[str]]:
    pytest = [
        sys.executable,
        "-m",
        "pytest",
        "--rootdir=" + str(ROOT),
        "-v",
        "--tb=short",
        *NODES,
        "--junitxml=" + str(junit),
        "-o",
        "junit_logging=system-out",
    ]
    run = 'test "$LG_PLACE" = tron && test -n "$LG_ENV" && HOPCAP_HW=1 ' + shlex.join(pytest)
    request = [
        "adi-lg",
        "request",
        "--part",
        "adrv9009zu11eg",
        "--carrier",
        "adrv2crr-fmc",
        "--mode",
        "reserve",
        "--wait",
        str(wait),
        "--run",
        run,
    ]
    return pytest, request


def validate_environment(env) -> None:
    for key in (
        "LG_ENV",
        "LG_PLACE",
        "LG_TOKEN",
        "LG_STATE",
        "IIO_URI",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "HOPCAP_HW",
    ):
        if env.get(key):
            raise ValueError(
                f"unset {key}: launch outside any managed reservation/direct-URI session"
            )


def require_env(env) -> None:
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    if missing:
        raise ValueError(f"missing environment: {', '.join(missing)}")


def validate_junit(path: Path, nodes) -> int:
    tree = ET.parse(path)
    cases = list(tree.iter("testcase"))
    expected = {n.split("::")[-1] for n in nodes}
    seen = {c.get("name") for c in cases}
    if seen != expected:
        raise ValueError(f"JUnit testcases {sorted(seen)} != expected {sorted(expected)}")
    for c in cases:
        for tag in ("failure", "error", "skipped"):
            if c.find(tag) is not None:
                raise ValueError(f"{c.get('name')} {tag}")
    return len(cases)


@contextmanager
def hardware_lock(timeout: float):
    import time

    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("timed out waiting for shared hardware lock") from None
                time.sleep(2)
        yield fd
    finally:
        os.close(fd)


def execute(args, junit: Path, request: list[str]) -> int:
    venv_bin = str(Path(sys.executable).parent)
    os.environ["PATH"] = venv_bin + os.pathsep + os.environ.get("PATH", "")
    for tool in ("adi-lg", "labgrid-client"):
        if shutil.which(tool) is None:
            raise ValueError(f"{tool} not found on PATH")
    validate_environment(os.environ)
    require_env(os.environ)
    env = dict(os.environ, LG_COORDINATOR=COORDINATOR, ADI_LG_COORDINATOR=COORDINATOR)
    # Preflight BEFORE taking the shared lock: it checks that the lock is free.
    pre = subprocess.run(
        [sys.executable, "-m", "hopcap.lab.preflight"], env=env, capture_output=True, text=True
    )
    print(pre.stdout)
    if pre.returncode != 0:
        raise ValueError("preflight failed")
    with hardware_lock(args.lock_timeout):
        junit.parent.mkdir(parents=True, exist_ok=True)
        if junit.exists():
            raise ValueError(f"refusing to overwrite {junit}")
        result = subprocess.run(request, env=env)
        if not junit.exists():
            raise ValueError(
                f"adi-lg exited {result.returncode} without producing {junit}: reservation/boot failed before pytest ran"
            )
        count = validate_junit(junit, NODES)
        out = Path(os.environ["HOPCAP_OUT"])
        ver = subprocess.run(
            [sys.executable, str(ROOT / "scripts/verify_evidence.py"), "run", str(out)],
            capture_output=True,
            text=True,
        )
        print(ver.stdout, ver.stderr)
        show = subprocess.run(
            ["labgrid-client", "-p", "tron", "show"], env=env, capture_output=True, text=True
        ).stdout
        if "acquired: None" not in show:
            raise ValueError("tron still acquired after adi-lg returned")
        if result.returncode != 0:
            raise ValueError(
                f"adi-lg failed with exit status {result.returncode}; JUnit alone is insufficient"
            )
        if ver.returncode != 0:
            raise ValueError("evidence verification failed")
        print(f"hopcap hardware run: {count} tests passed; evidence {out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", "--dry-run", dest="inspect", action="store_true")
    mode.add_argument("--run", action="store_true")
    ap.add_argument("--junit", type=Path, default=Path("junit-hopcap-tron.xml"))
    ap.add_argument("--wait", type=int, default=1800)
    ap.add_argument("--lock-timeout", type=float, default=1800)
    args = ap.parse_args(argv)
    pytest_argv, request = commands(args.junit.resolve(), args.wait)
    if args.inspect:
        json.dump(
            {
                "pytest": pytest_argv,
                "request": request,
                "nodes": list(NODES),
                "lock": str(LOCK),
                "coordinator": COORDINATOR,
                "required_env": list(REQUIRED_ENV),
                "optional_env": list(OPTIONAL_ENV),
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0
    try:
        return execute(args, args.junit.resolve(), request)
    except ValueError as exc:
        print(f"zu11eg_hopcap: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
