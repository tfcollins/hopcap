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


def bitstream_dir(
    tmp_path: Path, *, timing_ok=True, netlist_ok=True, patch_queue=None, port=None
) -> Path:
    """port=None writes evidence as it was before the port option: no physical_port, QSFP."""
    d = tmp_path / "bitstream"
    d.mkdir()
    bit = _write(d / "system_top.bit", b"BIT" * 100)
    xsa = _write(d / "system_top.xsa", b"XSA" * 100)
    manifest = {
        "schema": "hopcap-workspace-manifest/v1",
        "pins": PINS,
        "patch_queue": patch_queue or [],
        "contract_checks": sorted(ve.PORT_CONTRACTS[port or "qsfp"]),
        "adi_hdl_head": PINS["adi_hdl"]["revision"],
        "corundum_head": PINS["corundum"]["revision"],
        "files": {"library/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v": "0" * 64},
    }
    if port:
        manifest["physical_port"] = port
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
        + "check-sfp-rx-netlist: Stock RX DMA connected; SFP taps raw RX0 I/Q and valid: PASS\n"
        + ("check_hop_netlist: OK\n" if netlist_ok else "check_hop_netlist: 2 error(s)\n")
        + (f"physical_port={port}\n" if port else "")
        + f"adi_hdl={PINS['adi_hdl']['revision']}\ncorundum={PINS['corundum']['revision']}\nhopcap=abc\n"
        + f"bitstream_sha256={bit}\nvivado=vivado v2025.1\n"
    )
    return d


def test_bitstream_evidence_passes_when_consistent(tmp_path):
    assert ve.verify_bitstream(bitstream_dir(tmp_path)) == []


def test_sfp_bitstream_needs_only_the_dual_talise_check(tmp_path):
    assert ve.verify_bitstream(bitstream_dir(tmp_path, port="sfp")) == []


def test_qsfp_bitstream_still_needs_the_qsfp_contract(tmp_path):
    d = bitstream_dir(tmp_path, port="qsfp")
    man = json.loads((d / "MANIFEST.json").read_text())
    man["contract_checks"] = ["configure_dual_talise_links.py"]
    _rewrite_manifest(d, man)
    assert any("qsfp contract" in e for e in ve.verify_bitstream(d))


def test_summary_and_manifest_must_agree_on_the_port(tmp_path):
    d = bitstream_dir(tmp_path, port="sfp")
    (d / "summary.txt").write_text(
        (d / "summary.txt").read_text().replace("physical_port=sfp", "physical_port=qsfp")
    )
    assert any("physical_port" in e for e in ve.verify_bitstream(d))


def test_a_failed_rx_netlist_check_fails_the_bitstream(tmp_path):
    d = bitstream_dir(tmp_path, port="sfp")
    (d / "summary.txt").write_text(
        (d / "summary.txt").read_text().replace(": PASS", ": ValueError: tied off")
    )
    assert any("check-sfp-rx-netlist" in e for e in ve.verify_bitstream(d))


def _rewrite_manifest(d: Path, man: dict) -> None:
    digest = _write(d / "MANIFEST.json", json.dumps(man).encode())
    lines = [
        ln if not ln.endswith("MANIFEST.json") else f"{digest}  MANIFEST.json"
        for ln in (d / "SHA256SUMS").read_text().splitlines()
    ]
    (d / "SHA256SUMS").write_text("\n".join(lines) + "\n")


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


def boot_dir(tmp_path: Path, port=None) -> Path:
    d = tmp_path / f"boot-{port}"
    d.mkdir()
    sums = {}
    for name in ("BOOT.BIN", "Image", "system.dtb", "hopcap.dtbo", "vrt49-framer-control.dtbo"):
        sums[name] = _write(d / name, name.encode() * 10)
    (d / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in sums.items()))
    clock_source, output = ve.PORT_CLOCK[port or "qsfp"]
    prov = {
        "platform": f"adrv9009-zu11eg-{port or 'qsfp'}",
        "linux_source_sha": PINS["linux"]["revision"],
        f"{port or 'qsfp'}_clock_source": clock_source,
        "tick_driver": "in-tree obj-y",
        "dtb_checks": {
            f"{output}_output_clk": True,
            "tick_node": True,
            "hop_node": True,
            "framer_node": True,
        },
    }
    if port:
        prov["physical_port"] = port
    (d / "boot-provenance.json").write_text(json.dumps(prov))
    return d


def test_sfp_boot_evidence_requires_the_q1a_refclk(tmp_path):
    d = boot_dir(tmp_path, "sfp")
    assert ve.verify_boot(d) == []
    prov = json.loads((d / "boot-provenance.json").read_text())
    prov["sfp_clock_source"] = "AD9545_OUT1_B_Q1B"
    del prov["dtb_checks"]["q1a_output_clk"]
    prov["dtb_checks"]["q1b_output_clk"] = True
    (d / "boot-provenance.json").write_text(json.dumps(prov))
    errs = ve.verify_boot(d)
    assert any("Q1A" in e for e in errs) and any("q1a_output_clk" in e for e in errs)


def test_boot_platform_must_match_the_port(tmp_path):
    d = boot_dir(tmp_path, "sfp")
    prov = json.loads((d / "boot-provenance.json").read_text())
    prov["platform"] = "adrv9009-zu11eg-qsfp"
    (d / "boot-provenance.json").write_text(json.dumps(prov))
    assert any("platform" in e for e in ve.verify_boot(d))


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


# ---------------------------------------------------------------- timed-dma profile (P3b G1)


def tdma_bitstream_dir(tmp_path: Path) -> Path:
    d = bitstream_dir(tmp_path, port="sfp")
    man = json.loads((d / "MANIFEST.json").read_text())
    man["profile"] = "timed-dma"
    man["build_env"] = {"VRT_DAC_TX_ENABLE": "0"}
    man["files"].update({rel: "0" * 64 for rel in ve.TDMA_MANIFEST_FILES})
    _rewrite_manifest(d, man)
    (d / "summary.txt").write_text(
        (d / "summary.txt")
        .read_text()
        .replace(
            "check-sfp-rx-netlist: Stock RX DMA connected; SFP taps raw RX0 I/Q and valid: PASS\n",
            "check-sfp-rx-netlist: SKIPPED (profile timed-dma: by design)\n",
        )
        .replace(
            "check_hop_netlist: OK\n",
            "check_hop_netlist: profile timed-dma: OK\ncheck_hop_netlist: OK\n",
        )
        + "profile=timed-dma\n"
    )
    return d


def test_timed_dma_evidence_passes_with_the_rx_check_skipped(tmp_path):
    assert ve.verify_bitstream(tdma_bitstream_dir(tmp_path)) == []


def test_a_skipped_rx_check_is_refused_on_the_default_profile(tmp_path):
    d = tdma_bitstream_dir(tmp_path)
    man = json.loads((d / "MANIFEST.json").read_text())
    man["profile"] = "default"
    _rewrite_manifest(d, man)
    errs = ve.verify_bitstream(d)
    assert any("check-sfp-rx-netlist did not report PASS" in e for e in errs)
    assert any("summary profile" in e for e in errs)


def test_timed_dma_evidence_needs_its_checks_env_and_files(tmp_path):
    d = tdma_bitstream_dir(tmp_path)
    (d / "summary.txt").write_text(
        (d / "summary.txt").read_text().replace("check_hop_netlist: profile timed-dma: OK\n", "")
    )
    man = json.loads((d / "MANIFEST.json").read_text())
    man["build_env"] = {}
    del man["files"]["library/tick_dma_gate/tick_dma_gate.v"]
    _rewrite_manifest(d, man)
    errs = ve.verify_bitstream(d)
    assert any("timed-dma profile OK" in e for e in errs)
    assert any("VRT_DAC_TX_ENABLE" in e for e in errs)
    assert any("tick_dma_gate.v" in e for e in errs)


def tdma_jtag_boot_dir(tmp_path: Path) -> Path:
    """build_boot_artifacts.sh --profile timed-dma --skip-boot-bin --xsa ...: a JTAG boot set."""
    d = tmp_path / "boot-timed-dma"
    d.mkdir(parents=True)
    sums = {}
    for name in (
        "system_top.bit",
        "psu_init.tcl",
        "system_top.xsa",
        "Image",
        "system.dtb",
        "hopcap.dtbo",
        "vrt49-framer-control.dtbo",
    ):
        sums[name] = _write(d / name, name.encode() * 10)
    (d / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in sums.items()))
    clock_source, output = ve.PORT_CLOCK["sfp"]
    prov = {
        "platform": "adrv9009-zu11eg-sfp",
        "physical_port": "sfp",
        "profile": "timed-dma",
        "linux_source_sha": PINS["linux"]["revision"],
        "sfp_clock_source": clock_source,
        "xsa_sha256": sums["system_top.xsa"],
        "tick_driver": "in-tree obj-y",
        "dtb_checks": {
            f"{output}_output_clk": True,
            **{name: True for name in ve.BOOT_DTB_CHECKS["timed-dma"]},
        },
    }
    (d / "boot-provenance.json").write_text(json.dumps(prov))
    return d


def _edit_prov(d: Path, **changes) -> None:
    prov = json.loads((d / "boot-provenance.json").read_text())
    prov.update(changes)
    (d / "boot-provenance.json").write_text(json.dumps(prov))


def test_timed_dma_jtag_boot_set_passes_without_boot_bin(tmp_path):
    assert ve.verify_boot(tdma_jtag_boot_dir(tmp_path)) == []


def test_jtag_boot_set_needs_both_inputs_hashed(tmp_path):
    d = tdma_jtag_boot_dir(tmp_path)
    (d / "psu_init.tcl").unlink()
    assert any("psu_init.tcl missing" in e for e in ve.verify_boot(d))
    d2 = tdma_jtag_boot_dir(tmp_path / "b")
    (d2 / "system_top.bit").write_bytes(b"other")
    assert any("system_top.bit sha256 mismatch" in e for e in ve.verify_boot(d2))


def test_boot_set_without_boot_bin_or_jtag_inputs_fails(tmp_path):
    d = boot_dir(tmp_path, "sfp")
    (d / "BOOT.BIN").unlink()
    assert any("BOOT.BIN missing" in e for e in ve.verify_boot(d))


def test_jtag_xsa_must_be_the_one_provenance_records(tmp_path):
    d = tdma_jtag_boot_dir(tmp_path)
    _edit_prov(d, xsa_sha256="0" * 64)
    assert any("xsa_sha256" in e for e in ve.verify_boot(d))


def test_timed_dma_boot_requires_its_dtb_checks(tmp_path):
    for check in ("deframer_absent", "tx_dma_stock", "tick_gate_map"):
        d = tdma_jtag_boot_dir(tmp_path / check)
        prov = json.loads((d / "boot-provenance.json").read_text())
        del prov["dtb_checks"][check]
        (d / "boot-provenance.json").write_text(json.dumps(prov))
        assert any(f"dtb check {check} not recorded" in e for e in ve.verify_boot(d)), check


def test_timed_dma_boot_rejects_unknown_profile_and_qsfp(tmp_path):
    d = tdma_jtag_boot_dir(tmp_path)
    _edit_prov(d, profile="bogus")
    assert any("profile" in e for e in ve.verify_boot(d))
    d2 = tdma_jtag_boot_dir(tmp_path / "q")
    clock_source, output = ve.PORT_CLOCK["qsfp"]
    prov = json.loads((d2 / "boot-provenance.json").read_text())
    prov.update(
        physical_port="qsfp", platform="adrv9009-zu11eg-qsfp", qsfp_clock_source=clock_source
    )
    prov["dtb_checks"][f"{output}_output_clk"] = True
    (d2 / "boot-provenance.json").write_text(json.dumps(prov))
    assert any("SFP only" in e for e in ve.verify_boot(d2))
