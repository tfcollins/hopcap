#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fail-closed verifier for hopcap evidence directories (pattern: tick/scripts/verify_tick_evidence.py).

verify_evidence.py bitstream DIR [--release]   build_bitstream.sh output
verify_evidence.py boot DIR                    build_boot_artifacts.sh output
verify_evidence.py run DIR                     hopcap run bundle (manifest + SHA256SUMS.json)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / "pins.json").read_text())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_sums(d: Path, errors: list[str], required: tuple[str, ...]) -> None:
    sums = d / "SHA256SUMS"
    if not sums.exists():
        errors.append("SHA256SUMS missing")
        return
    listed: dict[str, str] = {}
    for line in sums.read_text().splitlines():
        if line.strip():
            digest, name = line.split(None, 1)
            listed[name.strip()] = digest
    for name in required:
        if name not in listed:
            errors.append(f"{name} not listed in SHA256SUMS")
        elif not (d / name).exists():
            errors.append(f"{name} missing")
        elif sha256(d / name) != listed[name]:
            errors.append(f"{name} sha256 mismatch")


def _kv(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith(("check", "build")):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def verify_bitstream(d: Path, *, release: bool = False) -> list[str]:
    errors: list[str] = []
    _check_sums(d, errors, ("system_top.bit", "system_top.xsa", "MANIFEST.json"))
    summary = d / "summary.txt"
    if not summary.exists():
        return errors + ["summary.txt missing"]
    text = summary.read_text()
    if not re.search(r"Timing constraints are met", text):
        errors.append("timing check did not report 'Timing constraints are met'")
    if "check_hop_netlist: OK" not in text:
        errors.append("check_hop_netlist did not report OK")
    kv = _kv(text)
    for key in ("adi_hdl", "corundum"):
        if kv.get(key) != PINS[key]["revision"]:
            errors.append(f"{key} in summary ({kv.get(key)}) != pins.json {PINS[key]['revision']}")
    if (d / "system_top.bit").exists() and kv.get("bitstream_sha256") != sha256(
        d / "system_top.bit"
    ):
        errors.append("bitstream_sha256 in summary does not match system_top.bit")
    if "v2025.1" not in kv.get("vivado", ""):
        errors.append(f"vivado version {kv.get('vivado')!r} is not 2025.1")
    man_path = d / "MANIFEST.json"
    if man_path.exists():
        man = json.loads(man_path.read_text())
        if man.get("adi_hdl_head") != PINS["adi_hdl"]["revision"]:
            errors.append("MANIFEST adi_hdl_head != pins.json")
        if release and man.get("patch_queue"):
            errors.append(f"release build has a non-empty patch queue: {man['patch_queue']}")
        if set(man.get("contract_checks", [])) < {
            "configure_dual_talise_links.py",
            "check-zu11eg-qsfp-contract.py",
        }:
            errors.append("MANIFEST lacks the vrt49 contract checks")
        if not any("vrt49_fhm_hop_ctrl" in f for f in man.get("files", {})):
            errors.append("MANIFEST does not list the hop controller RTL")
    return errors


def verify_boot(d: Path) -> list[str]:
    errors: list[str] = []
    _check_sums(
        d, errors, ("BOOT.BIN", "Image", "system.dtb", "hopcap.dtbo", "vrt49-framer-control.dtbo")
    )
    prov_path = d / "boot-provenance.json"
    if not prov_path.exists():
        return errors + ["boot-provenance.json missing"]
    prov = json.loads(prov_path.read_text())
    if prov.get("platform") != "adrv9009-zu11eg-qsfp":
        errors.append(f"platform {prov.get('platform')!r} != adrv9009-zu11eg-qsfp")
    if prov.get("linux_source_sha") != PINS["linux"]["revision"]:
        errors.append("linux_source_sha != pins.json")
    if prov.get("qsfp_clock_source") != "AD9545_OUT1_B_Q1B":
        errors.append("QSFP refclk is not AD9545 Q1B")
    for check, ok in (prov.get("dtb_checks") or {}).items():
        if not ok:
            errors.append(f"dtb check {check} failed")
    for required in ("q1b_output_clk", "tick_node", "hop_node", "framer_node"):
        if required not in (prov.get("dtb_checks") or {}):
            errors.append(f"dtb check {required} not recorded")
    return errors


def verify_run(d: Path) -> list[str]:
    errors: list[str] = []
    sums_path = d / "SHA256SUMS.json"
    if not sums_path.exists():
        return ["SHA256SUMS.json missing"]
    sums = json.loads(sums_path.read_text())
    for name, digest in sums.items():
        p = d / name
        if not p.exists():
            errors.append(f"{name} missing")
        elif sha256(p) != digest:
            errors.append(f"{name} sha256 mismatch")
    man_path = d / "manifest.json"
    if not man_path.exists():
        return errors + ["manifest.json missing"]
    man = json.loads(man_path.read_text())
    if man.get("schema") != "hopcap-manifest/v1":
        errors.append("manifest schema is not hopcap-manifest/v1")
    if man.get("disposition") not in ("ok", "incomplete", "invalid"):
        errors.append("manifest has no disposition")
    if not man.get("hops"):
        errors.append("manifest has no hops")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("kind", choices=("bitstream", "boot", "run"))
    ap.add_argument("dir", type=Path)
    ap.add_argument("--release", action="store_true")
    args = ap.parse_args()
    if args.kind == "bitstream":
        errors = verify_bitstream(args.dir, release=args.release)
    elif args.kind == "boot":
        errors = verify_boot(args.dir)
    else:
        errors = verify_run(args.dir)
    for e in errors:
        print(f"verify_evidence: {e}", file=sys.stderr)
    print(
        f"verify_evidence {args.kind} {args.dir}: {'OK' if not errors else f'{len(errors)} error(s)'}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
