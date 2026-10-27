# SPDX-License-Identifier: MIT
"""verify_evidence.py: fail-closed checks over bitstream / boot / run evidence directories."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_evidence as ve  # noqa: E402

PINS = json.loads((ROOT / "pins.json").read_text())


def _write(p: Path, data: bytes) -> str:
    p.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def bitstream_dir(tmp_path: Path, *, timing_ok=True, netlist_ok=True, patch_queue=None) -> Path:
    d = tmp_path / "bitstream"
    d.mkdir()
    bit = _write(d / "system_top.bit", b"BIT" * 100)
    xsa = _write(d / "system_top.xsa", b"XSA" * 100)
    manifest = {
        "schema": "hopcap-workspace-manifest/v1",
        "pins": PINS,
        "patch_queue": patch_queue or [],
        "contract_checks": ["configure_dual_talise_links.py", "check-zu11eg-qsfp-contract.py"],
        "adi_hdl_head": PINS["adi_hdl"]["revision"],
        "corundum_head": PINS["corundum"]["revision"],
        "files": {"library/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v": "0" * 64},
    }
    man = _write(d / "MANIFEST.json", json.dumps(manifest).encode())
    (d / "SHA256SUMS").write_text(
        f"{bit}  system_top.bit\n{xsa}  system_top.xsa\n{man}  MANIFEST.json\n"
    )
    (d / "summary.txt").write_text(
        "build: ws=x\n"
        + (
            "check-vivado-timing: Timing constraints are met\n"
            if timing_ok
            else "check-vivado-timing: FAILED\n"
        )
        + ("check_hop_netlist: OK\n" if netlist_ok else "check_hop_netlist: 2 error(s)\n")
        + f"adi_hdl={PINS['adi_hdl']['revision']}\ncorundum={PINS['corundum']['revision']}\nhopcap=abc\n"
        + f"bitstream_sha256={bit}\nvivado=vivado v2025.1\n"
    )
    return d


def test_bitstream_evidence_passes_when_consistent(tmp_path):
    assert ve.verify_bitstream(bitstream_dir(tmp_path)) == []


def test_bitstream_evidence_fails_on_hash_mismatch_or_failed_checks(tmp_path):
    d = bitstream_dir(tmp_path, timing_ok=False)
    (d / "system_top.bit").write_bytes(b"tampered")
    errs = ve.verify_bitstream(d)
    assert any("system_top.bit" in e for e in errs) and any("timing" in e.lower() for e in errs)


def test_release_mode_rejects_nonempty_patch_queue(tmp_path):
    d = bitstream_dir(tmp_path, patch_queue=["0001-x.patch"])
    assert ve.verify_bitstream(d) == []
    assert any("patch queue" in e for e in ve.verify_bitstream(d, release=True))


def test_bitstream_evidence_rejects_pin_drift(tmp_path):
    d = bitstream_dir(tmp_path)
    s = (d / "summary.txt").read_text().replace(PINS["adi_hdl"]["revision"], "f" * 40)
    (d / "summary.txt").write_text(s)
    assert any("adi_hdl" in e for e in ve.verify_bitstream(d))


def boot_dir(tmp_path: Path) -> Path:
    d = tmp_path / "boot"
    d.mkdir()
    sums = {}
    for name in ("BOOT.BIN", "Image", "system.dtb", "hopcap.dtbo", "vrt49-framer-control.dtbo"):
        sums[name] = _write(d / name, name.encode() * 10)
    (d / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in sums.items()))
    (d / "boot-provenance.json").write_text(
        json.dumps(
            {
                "platform": "adrv9009-zu11eg-qsfp",
                "linux_source_sha": PINS["linux"]["revision"],
                "qsfp_clock_source": "AD9545_OUT1_B_Q1B",
                "tick_driver": "in-tree obj-y",
                "dtb_checks": {
                    "q1b_output_clk": True,
                    "tick_node": True,
                    "hop_node": True,
                    "framer_node": True,
                },
            }
        )
    )
    return d


def test_boot_evidence_passes_and_fails_closed(tmp_path):
    d = boot_dir(tmp_path)
    assert ve.verify_boot(d) == []
    prov = json.loads((d / "boot-provenance.json").read_text())
    prov["dtb_checks"]["tick_node"] = False
    (d / "boot-provenance.json").write_text(json.dumps(prov))
    assert any("tick_node" in e for e in ve.verify_boot(d))


def test_cli_exit_codes(tmp_path):
    import subprocess

    d = bitstream_dir(tmp_path)
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_evidence.py"), "bitstream", str(d)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and "OK" in r.stdout
    (d / "system_top.bit").write_bytes(b"x")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_evidence.py"), "bitstream", str(d)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1
