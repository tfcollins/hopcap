#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Boot tron from its SD card with NO artifact staging and report what is there.

Run inside an adi-lg reservation (LG_ENV set).  This is the first thing to do in a lab session:
it proves the SD boot path works before any exact-artifact staging, and records the stock
/boot checksums so a restore can be verified against an independent copy.

  adi-lg request --part adrv9009zu11eg --carrier adrv2crr-fmc --mode reserve --wait 600 \
    --run 'python3 scripts/tron_probe.py --out probe.json'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hopcap.lab.boot import BootArtifacts, TronSession  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("tron-probe.json"))
    ap.add_argument("--keep-powered", action="store_true")
    args = ap.parse_args()
    env = dict(os.environ)
    env["VRT49_CORUNDUM_BOOT"] = env.get("VRT49_CORUNDUM_BOOT", "sd")
    for k in list(env):
        if k.startswith("VRT49_CORUNDUM_") and k.endswith(("_URL", "_SHA256")):
            del env[k]  # probe never stages
    artifacts = BootArtifacts.from_env(env)
    session = TronSession(artifacts)
    report: dict = {"mode": artifacts.mode}
    try:
        shell = session.boot_exact()
        for key, cmd in {
            "proc_version": "cat /proc/version",
            "boot_sums": "sha256sum /boot/BOOT.BIN /boot/Image /boot/system.dtb",
            "boot_listing": "ls -la /boot | head -40",
            "stock_backup_dirs": "ls -d /boot/vrt49-stock* 2>/dev/null || true",
            "ip": "ip -4 -o addr show scope global",
            "default_route": "ip route show default",
            "iio_devices": "cat /sys/bus/iio/devices/*/name 2>/dev/null",
            "python3": "python3 --version 2>&1",
            "wget": "command -v wget || echo missing",
            "qsfp_present": "cat /sys/class/net/*/address 2>/dev/null | head -5",
        }.items():
            try:
                report[key] = shell.run(cmd, timeout_s=60).strip()
            except Exception as exc:  # noqa: BLE001
                report[key] = f"ERROR: {exc}"
    finally:
        if args.keep_powered:
            report["note"] = "left powered (--keep-powered)"
        else:
            session.restore_and_power_off()
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
