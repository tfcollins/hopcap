# SPDX-License-Identifier: MIT
"""Read-only nemo-side gate for a hopcap hardware run (shape follows vrt49's
scripts/preflight-zu11eg-hil.py). Checks the XL710 QSFP receiver NIC, its 192.0.2.2/24
address, the labgrid place tags, the shared hardware lock and coordinator reachability.
Never acquires, boots or changes links.

`--boot-mode` prints only `<jtag|sd> <boot-strategy tag>`: the place's `boot-strategy` tag is the
lab's record of how tron is strapped, and the run wrapper picks the boot path from it. The raw
`show` output carries the power outlet's bearer token, so only the place's own lines are parsed
and none of it is ever printed."""

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
# boot-strategy tag -> hopcap boot path. Whoever re-straps tron's boot-mode switches changes the tag.
BOOT_MODES = {"BootZynqMPJTAG": "jtag", "BootFPGASoC": "sd"}
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
    """192.0.2.2/24 must be deliverable to a socket from datagrams arriving on the receiver NIC.

    Either the address sits on the receiver interface itself, or (weak host model) it sits on
    another interface AND the receiver interface's rp_filter is 0.  Linux drops every packet
    arriving on an interface that has *no* IPv4 address when rp_filter is non-zero, loose mode
    included (fib_validate_source "last_resort"); measured on nemo 2026-10-23 as
    TcpExtIPReversePathFilter == received packets.
    """
    on_receiver = bool(
        re.search(rf"\binet {re.escape(RECEIVER_IPV4)}\b.*\b{re.escape(interface)}\b", raw)
    )
    anywhere = bool(re.search(rf"\binet {re.escape(RECEIVER_IPV4)}\b", raw))
    ok = on_receiver or (anywhere and rp_filter == 0)
    if on_receiver:
        note = None
    elif ok:
        note = "weak-host delivery via another interface (receiver rp_filter is 0)"
    elif anywhere:
        note = (
            f"{RECEIVER_IPV4} is on another interface and {interface} has rp_filter={rp_filter}: the kernel "
            f"will drop every datagram arriving on {interface}. Fix (root): "
            f"`sysctl -w net.ipv4.conf.{interface}.rp_filter=0` or `nmcli con up vrt49-qsfp` while the link is up"
        )
    else:
        note = f"{RECEIVER_IPV4} is not configured on any interface"
    return {
        "ok": ok,
        "expected": RECEIVER_IPV4,
        "interface": interface,
        "on_receiver_interface": on_receiver,
        "address_present": anywhere,
        "rp_filter": rp_filter,
        "note": note,
    }


class BootModeError(RuntimeError):
    pass


def _place_section(raw: str) -> tuple[bool, dict[str, str], str | None]:
    """(exact place header, tags, acquired) from the place's own indented lines only."""
    lines = raw.splitlines()
    exact = bool(lines and lines[0] == f"Place '{PLACE}':")
    tags: dict[str, str] = {}
    acquired = None
    for line in lines[1:]:
        if line and not line.startswith("  "):
            break  # resource details (with outlet credentials) follow the place section
        if line.startswith("  tags: "):
            for item in line[8:].split(", "):
                k, _, v = item.partition("=")
                tags[k] = v
        elif line.startswith("  acquired: "):
            acquired = line.split(": ", 1)[1].strip()
            acquired = None if acquired == "None" else acquired
    return exact, tags, acquired


def validate_place(raw: str) -> dict:
    exact, tags, acquired = _place_section(raw)
    matches = {k: tags.get(k) == v for k, v in TAGS.items()}
    strategy = tags.get("boot-strategy")
    return {
        "ok": exact and all(matches.values()),
        "exact_place": exact,
        "tag_matches": matches,
        "boot_strategy": strategy,
        "boot_mode": BOOT_MODES.get(strategy),
        "acquired": acquired,
    }


def boot_mode_from_show(raw: str) -> dict:
    exact, tags, _ = _place_section(raw)
    if not exact:
        raise BootModeError(
            f"labgrid did not describe place {PLACE}; pass --boot jtag or --boot sd"
        )
    strategy = tags.get("boot-strategy")
    if not strategy:
        raise BootModeError(
            f"place {PLACE} has no boot-strategy tag; pass --boot jtag or --boot sd"
        )
    if strategy not in BOOT_MODES:
        known = ", ".join(f"{k} ({v})" for k, v in BOOT_MODES.items())
        raise BootModeError(
            f"place {PLACE} boot-strategy={strategy} is not one of {known}; "
            "pass --boot jtag or --boot sd"
        )
    return {"mode": BOOT_MODES[strategy], "boot_strategy": strategy}


def find_labgrid_client() -> str | None:
    """On PATH, else beside this interpreter (the launcher runs a venv python, not an activated venv)."""
    found = shutil.which("labgrid-client")
    if found:
        return found
    beside = Path(sys.executable).parent / "labgrid-client"
    return str(beside) if beside.is_file() and os.access(beside, os.X_OK) else None


def show_place(lgc: str, *, timeout: float = 15.0, run=None):
    # LG_ENV/LG_STATE (set inside a reservation) can activate a strategy even for `show`.
    env = {k: v for k, v in os.environ.items() if not k.startswith("LG_")}
    env["LG_COORDINATOR"] = COORDINATOR
    return (run or subprocess.run)(
        [lgc, "-p", PLACE, "show"], capture_output=True, text=True, timeout=timeout, env=env
    )


def detect_boot_mode(*, labgrid_client_path: str | None = None, timeout: float = 15.0, run=None):
    lgc = labgrid_client_path or find_labgrid_client()
    if not lgc:
        raise BootModeError("labgrid-client not found; pass --boot jtag or --boot sd")
    try:
        r = show_place(lgc, timeout=timeout, run=run)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootModeError(
            f"labgrid-client -p {PLACE} show: {type(exc).__name__}; pass --boot jtag or --boot sd"
        ) from None
    if r.returncode != 0:
        raise BootModeError(
            f"labgrid-client -p {PLACE} show failed (rc {r.returncode}); pass --boot jtag or --boot sd"
        )
    return boot_mode_from_show(r.stdout)


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
                ["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=timeout
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            raw = ""
        rp = Path(f"/proc/sys/net/ipv4/conf/{iface}/rp_filter")
        rp_val = int(rp.read_text().strip()) if rp.exists() else None
        sections["address"] = validate_receiver_address(raw, iface, rp_val)
    else:
        sections["address"] = {"ok": False, "reason": "no receiver interface"}
    lgc = labgrid_client or find_labgrid_client()
    if lgc:
        try:
            sections["place"] = validate_place(show_place(lgc, timeout=timeout).stdout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            sections["place"] = {"ok": False, "reason": str(exc)}
    else:
        sections["place"] = {"ok": False, "reason": "labgrid-client not installed"}
    sections["lock"] = lock_state()
    sections["coordinator"] = coordinator_reachable()
    if iface:
        sections["nic_ring"] = nic_ring(iface)
    return summarize(sections)


def nic_ring(interface: str, timeout: float = 5.0) -> dict:
    """Report-only: the RX ring size. 512 (i40e default) lost ~90 of 683 packets per 8.4 Gb/s
    burst on 2026-10-23; vrt49's QSFP qualification used 8160 (`ethtool -G <if> rx 8160`, root)."""
    out = {"ok": True, "interface": interface, "rx_ring": None, "recommended": 8160}
    try:
        raw = subprocess.run(
            ["ethtool", "-g", interface], capture_output=True, text=True, timeout=timeout
        ).stdout
        cur = raw.split("Current hardware settings:", 1)[-1]
        m = re.search(r"^RX:\s+(\d+)", cur, re.M)
        out["rx_ring"] = int(m.group(1)) if m else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    out["note"] = (
        None
        if (out["rx_ring"] or 0) >= 4096
        else "small RX ring: expect ring loss in full-rate bursts"
    )
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--boot-mode"]:
        try:
            r = detect_boot_mode()
        except BootModeError as exc:
            print(exc, file=sys.stderr)
            return 2
        print(f"{r['mode']} {r['boot_strategy']}")
        return 0
    if args:
        print("usage: python -m hopcap.lab.preflight [--boot-mode]", file=sys.stderr)
        return 2
    result = preflight()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
