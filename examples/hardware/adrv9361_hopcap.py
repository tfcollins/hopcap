#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Shape follows examples/hardware/zu11eg_hopcap.py (environment guards, --inspect, evidence
# verification) and tick's scripts/run_lablp_repeat_hardware_test.sh (the proven lablp flow:
# adi-lg request --mode uri --power-down --run, token-safe place listing, released check).
"""Run a hopcap board session on lablp (ADRV9361-Z7035 + ADRV1CRR-FMC) through adi-lg.

--inspect / --dry-run  print the adi-lg argv, the board-side session command and the required
                       environment (offline)
--run                  check nemo's receiver (enp7s0, 192.168.2.10/24), let adi-lg reserve and boot
                       lablp's stock SD card, run ``python -m hopcap.lab.lablp`` against IIO_URI
                       (runtime PL load, radio, RX buffer, link, time base, then the hop plan
                       unless HOPCAP_LABLP_NO_RUN=1), then require the place released, the board
                       powered down and the run bundle verified

No hardware lock is taken: labgrid's reservation serialises lablp, and nemo's enp7s0 belongs to
lablp alone (tron's corundum lock guards tron and the 10G NICs, which this run does not touch).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COORDINATOR = "10.0.0.41:20408"
PLACE = "lablp"
PART, CARRIER = "adrv9361z7035", "adrv1crr-fmc"
TAGS = {"daughter-board": PART, "carrier": CARRIER}
REQUIRED_ENV = ("HOPCAP_OUT", "HOPCAP_LABLP_BIN", "HOPCAP_TICK_MODULE", "HOPCAP_VRT49_MODULE")
OPTIONAL_ENV = (
    "HOPCAP_PLAN",
    "HOPCAP_TARGET_PASSWORD",
    "HOPCAP_RX_CPU",
    "HOPCAP_LABLP_NO_RUN",
    "HOPCAP_COMMIT",
)
SECRET = re.compile(r"token|bearer|eyJ[A-Za-z0-9_-]{8,}", re.IGNORECASE)


def session_command(evidence: Path, *, no_run: bool, rx_cpu: str | None = None) -> str:
    argv = [sys.executable, "-m", "hopcap.lab.lablp", "--evidence", str(evidence)]
    if no_run:
        argv.append("--no-run")
    if rx_cpu:
        argv += ["--rx-cpu", str(rx_cpu)]
    # adi-lg exports IIO_URI=ip:<board> in --mode uri; nothing else identifies the board.
    return 'test -n "$IIO_URI" && HOPCAP_HW=1 HOPCAP_PLATFORM=adrv9361 ' + shlex.join(argv)


def commands(evidence: Path, wait: int, *, no_run: bool = False, rx_cpu: str | None = None):
    inner = session_command(evidence, no_run=no_run, rx_cpu=rx_cpu)
    request = [
        "adi-lg",
        "request",
        "--part",
        PART,
        "--carrier",
        CARRIER,
        "--mode",
        "uri",
        "--wait",
        str(wait),
        "--power-down",
        "--run",
        inner,
    ]
    return inner, request


def validate_environment(env) -> None:
    for key in ("LG_ENV", "LG_PLACE", "LG_TOKEN", "LG_STATE", "IIO_URI", "HOPCAP_HW"):
        if env.get(key):
            raise ValueError(
                f"unset {key}: launch outside any managed reservation/direct-URI session"
            )


def require_env(env) -> None:
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    if missing:
        raise ValueError(f"missing environment: {', '.join(missing)}")
    for k in REQUIRED_ENV[1:]:
        if not Path(env[k]).is_file():
            raise ValueError(f"{k}={env[k]} is not a file")
    if env.get("HOPCAP_PLAN") and not Path(env["HOPCAP_PLAN"]).is_file():
        raise ValueError(f"HOPCAP_PLAN={env['HOPCAP_PLAN']} is not a file")


def scrub(text: str) -> str:
    """Drop every line that could carry the power outlet's bearer token."""
    return "".join(line for line in text.splitlines(keepends=True) if not SECRET.search(line))


def place_header(show: str) -> str:
    """The place's own lines only: resource details (outlet credentials) follow them."""
    out = []
    for line in show.splitlines():
        if line.startswith("Matching resource"):
            break
        out.append(line)
    return scrub("\n".join(out) + "\n")


def check_place(header: str) -> None:
    if not header.startswith(f"Place '{PLACE}':"):
        raise ValueError(f"labgrid did not describe place {PLACE}")
    for k, v in TAGS.items():
        if f"{k}={v}" not in header:
            raise ValueError(f"place {PLACE} is not tagged {k}={v}")


def released(header: str) -> bool:
    return any(line.strip() == "acquired: None" for line in header.splitlines())


def receiver_preflight() -> dict:
    """nemo side, read-only: enp7s0 (by MAC, e1000e) and 192.168.2.10/24 on it."""
    from hopcap import link
    from hopcap.lab import preflight

    rx = link.platform_receiver("adrv9361")
    nic = preflight.receiver_interface(rx=rx)
    iface = nic.get("interface")
    if iface:
        raw = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True
        ).stdout
        rp = preflight.read_rp_filter(iface)
        addr = preflight.validate_receiver_address(
            raw, iface, rp and rp["effective"], nm_profile=rx.nm_profile, expected=rx.ipv4
        )
    else:
        addr = {"ok": False, "reason": "no receiver interface"}
    return preflight.summarize(
        {"receiver": nic, "address": addr, "coordinator": preflight.coordinator_reachable()}
    )


def stream_filtered(argv: list[str], env, log_path: Path) -> int:
    """Run ``argv``, echoing and logging its output line by line with secrets dropped."""
    with (
        log_path.open("w") as log,
        subprocess.Popen(
            argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        ) as proc,
    ):
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = scrub(line)
            log.write(clean)
            sys.stdout.write(clean)
        return proc.wait()


def labgrid_show(env) -> str:
    return subprocess.run(
        ["labgrid-client", "-x", COORDINATOR, "-p", PLACE, "show"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def execute(args) -> int:
    venv_bin = str(Path(sys.executable).parent)
    os.environ["PATH"] = venv_bin + os.pathsep + os.environ.get("PATH", "")
    for tool in ("adi-lg", "labgrid-client", "iio_attr", "dtc", "modinfo"):
        if shutil.which(tool) is None:
            raise ValueError(f"{tool} not found on PATH")
    validate_environment(os.environ)
    require_env(os.environ)
    evidence = Path(os.environ["HOPCAP_OUT"]).resolve()
    if evidence.exists() and any(evidence.iterdir()):
        raise ValueError(f"refusing to reuse a non-empty evidence directory {evidence}")
    evidence.mkdir(parents=True, exist_ok=True)
    pre = receiver_preflight()
    (evidence / "preflight.json").write_text(json.dumps(pre, indent=2) + "\n")
    if not pre["ok"]:
        raise ValueError(f"nemo receiver preflight failed (see {evidence / 'preflight.json'})")
    env = dict(
        os.environ,
        LG_COORDINATOR=COORDINATOR,
        ADI_LG_COORDINATOR=COORDINATOR,
        PYTHONPATH=os.pathsep.join(
            [
                str(ROOT / "src"),
                str(ROOT / "third_party/tick/src"),
                os.environ.get("PYTHONPATH", ""),
            ]
        ),
    )
    header = place_header(labgrid_show(env))
    (evidence / "labgrid-place.log").write_text(header)
    check_place(header)
    _inner, request = commands(
        evidence,
        args.wait,
        no_run=os.environ.get("HOPCAP_LABLP_NO_RUN") == "1",
        rx_cpu=os.environ.get("HOPCAP_RX_CPU"),
    )
    returncode = stream_filtered(request, env, evidence / "labgrid-boot.log")
    after = place_header(labgrid_show(env))
    (evidence / "labgrid-release.log").write_text(after)
    if not released(after):
        raise ValueError(f"place {PLACE} is still acquired after adi-lg returned")
    session = evidence / "session.json"
    if not session.exists():
        raise ValueError(
            f"adi-lg exited {returncode} without a session record: reservation/boot failed"
        )
    if returncode != 0:
        raise ValueError(f"adi-lg/session failed with exit status {returncode} (see session.json)")
    record = json.loads(session.read_text())
    if "run" in record:
        ver = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/verify_evidence.py"),
                "run",
                str(evidence / "run"),
            ],
            capture_output=True,
            text=True,
        )
        print(ver.stdout, ver.stderr)
        if ver.returncode != 0:
            raise ValueError("run evidence verification failed")
    print(f"adrv9361 hopcap session passed; evidence {evidence}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", "--dry-run", dest="inspect", action="store_true")
    mode.add_argument("--run", action="store_true")
    ap.add_argument("--wait", type=int, default=1800)
    args = ap.parse_args(argv)
    if args.inspect:
        evidence = Path(os.environ.get("HOPCAP_OUT") or "evidence-adrv9361")
        inner, request = commands(evidence.resolve(), args.wait)
        json.dump(
            {
                "request": request,
                "session": inner,
                "place": PLACE,
                "coordinator": COORDINATOR,
                "tags": TAGS,
                "required_env": list(REQUIRED_ENV),
                "optional_env": list(OPTIONAL_ENV),
                "lock": None,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0
    try:
        return execute(args)
    except ValueError as exc:
        print(f"adrv9361_hopcap: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
