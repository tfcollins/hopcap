# SPDX-License-Identifier: MIT
"""Read-only nemo-side gate for a hopcap hardware run (shape follows vrt49's
scripts/preflight-zu11eg-hil.py). Checks the XL710 QSFP receiver NIC, its 192.0.2.2/24
address, the labgrid place tags, the shared hardware lock and coordinator reachability.
Never acquires, boots or changes links."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

COORDINATOR = "10.0.0.41:20408"
PLACE = "tron"
TAGS = {"daughter-board": "adrv9009zu11eg", "carrier": "adrv2crr-fmc", "runner": "hw-nemo"}
RECEIVER_MAC = "64:9d:99:6f:a9:6a"  # nemo XL710 enp4s0f0np0 (QSFP lane-0 DAC to tron)
RECEIVER_DRIVER = "i40e"
RECEIVER_IPV4 = "192.0.2.2/24"
LOCK = Path("/tmp/vrt49-corundum-hardware.lock")


def receiver_interface(root: Path = Path("/sys/class/net")) -> dict:
    for iface in sorted(p for p in root.iterdir() if p.is_dir()):
        addr = iface / "address"
        if not addr.exists() or addr.read_text().strip().lower() != RECEIVER_MAC:
            continue
        driver_link = iface / "device" / "driver"
        driver = os.path.basename(os.readlink(driver_link)) if driver_link.is_symlink() else None
        operstate = (
            (iface / "operstate").read_text().strip() if (iface / "operstate").exists() else None
        )
        # Link state is reported, not gated: the QSFP carrier only exists while the FPGA drives
        # it, i.e. after the board has booted the image under test.
        return {
            "ok": driver == RECEIVER_DRIVER,
            "interface": iface.name,
            "mac": RECEIVER_MAC,
            "driver": driver,
            "operstate": operstate,
            "link_up": operstate == "up",
        }
    return {
        "ok": False,
        "interface": None,
        "mac": RECEIVER_MAC,
        "reason": "no interface with the receiver MAC",
    }


def validate_receiver_address(raw: str, interface: str, rp_filter: int | None = None) -> dict:
    """192.0.2.2/24 must be a local address.  On the receiver interface is ideal; on another
    interface (vrt49's SFP profile owns it when the QSFP profile cannot activate without carrier)
    is acceptable under the weak host model as long as the receiver interface's rp_filter is not
    strict (1): the kernel then delivers datagrams arriving on the QSFP port to a socket bound
    to 192.0.2.2."""
    on_receiver = bool(
        re.search(rf"\binet {re.escape(RECEIVER_IPV4)}\b.*\b{re.escape(interface)}\b", raw)
    )
    anywhere = bool(re.search(rf"\binet {re.escape(RECEIVER_IPV4)}\b", raw))
    strict = rp_filter == 1
    ok = on_receiver or (anywhere and not strict)
    return {
        "ok": ok,
        "expected": RECEIVER_IPV4,
        "interface": interface,
        "on_receiver_interface": on_receiver,
        "address_present": anywhere,
        "rp_filter": rp_filter,
        "note": None
        if on_receiver
        else (
            "weak-host delivery via another interface"
            if ok
            else "address missing or rp_filter strict"
        ),
    }


def validate_place(raw: str) -> dict:
    lines = raw.splitlines()
    exact = bool(lines and lines[0] == f"Place '{PLACE}':")
    tags: dict[str, str] = {}
    acquired = None
    for line in lines[1:]:
        if line.startswith("  tags: "):
            for item in line[8:].split(", "):
                k, _, v = item.partition("=")
                tags[k] = v
        elif line.startswith("  acquired: "):
            acquired = line.split(": ", 1)[1].strip()
            acquired = None if acquired == "None" else acquired
    matches = {k: tags.get(k) == v for k, v in TAGS.items()}
    return {
        "ok": exact and all(matches.values()),
        "exact_place": exact,
        "tag_matches": matches,
        "acquired": acquired,
    }


def lock_state(lock: Path = LOCK) -> dict:
    if not lock.exists():
        return {"free": True, "path": str(lock)}
    fd = os.open(lock, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"free": False, "path": str(lock)}
        fcntl.flock(fd, fcntl.LOCK_UN)
        return {"free": True, "path": str(lock)}
    finally:
        os.close(fd)


def coordinator_reachable(coordinator: str = COORDINATOR, timeout: float = 5.0) -> dict:
    host, port = coordinator.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"ok": True, "coordinator": coordinator}
    except OSError as exc:
        return {"ok": False, "coordinator": coordinator, "reason": str(exc)}


def summarize(sections: dict) -> dict:
    ok = all(s.get("ok", s.get("free", False)) for s in sections.values())
    return {"ok": ok, **sections}


def preflight(*, labgrid_client: str | None = None, timeout: float = 15.0) -> dict:
    sections: dict = {}
    sections["receiver"] = receiver_interface()
    iface = sections["receiver"].get("interface")
    if iface:
        try:
            raw = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "dev", iface],
                capture_output=True,
                text=True,
                timeout=timeout,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            raw = ""
        sections["address"] = validate_receiver_address(raw, iface)
    else:
        sections["address"] = {"ok": False, "reason": "no receiver interface"}
    lgc = labgrid_client or shutil.which("labgrid-client")
    if lgc:
        try:
            raw = subprocess.run(
                [lgc, "-p", PLACE, "show"],
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "LG_COORDINATOR": COORDINATOR},
            ).stdout
            sections["place"] = validate_place(raw)
        except (OSError, subprocess.TimeoutExpired) as exc:
            sections["place"] = {"ok": False, "reason": str(exc)}
    else:
        sections["place"] = {"ok": False, "reason": "labgrid-client not installed"}
    sections["lock"] = lock_state()
    sections["coordinator"] = coordinator_reachable()
    return summarize(sections)


def main() -> int:
    result = preflight()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
