# SPDX-License-Identifier: MIT
"""scripts/build_bitstream.sh --post-only on an adrv9361 workspace, with stub vivado/bootgen:
the post-build gates run on the right project and the FPGA-manager .bin is produced."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build_bitstream.sh"
PINS = json.loads((ROOT / "pins.json").read_text())
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

FINAL_TIMING = """| Tool Version : Vivado v.2025.1
| Device       : 7z035i-fbg676
| Speed File   : -2L  PRODUCTION 1.12 2019-11-22
| Design State : Routed
All user specified timing constraints are met.
"""


@pytest.fixture
def ws(tmp_path):
    import assemble_workspace as aw
    from test_check_hop_netlist_adrv9361 import GATE_REPORT, NETLIST, WRAPPERS

    ws = tmp_path / "ws"
    hdl = ws / "hdl"
    proj = hdl / "projects/adrv9361z7035_vrt49_sfp"
    gen = proj / "adrv9361z7035_vrt49_sfp.gen/sources_1/bd/system"
    (gen / "synth").mkdir(parents=True)
    (gen / "synth/system.v").write_text(NETLIST)
    for cell, text in WRAPPERS.items():
        d = gen / "ip" / f"system_{cell}_0" / "synth"
        d.mkdir(parents=True)
        (d / f"system_{cell}_0.v").write_text(text)
    impl = proj / "adrv9361z7035_vrt49_sfp.runs/impl_1"
    impl.mkdir(parents=True)
    (impl / "system_top.bit").write_bytes(b"\x00\x09bitstream")
    (impl / "system_top_timing_summary_routed.rpt").write_text(FINAL_TIMING)
    sdk = proj / "adrv9361z7035_vrt49_sfp.sdk"
    sdk.mkdir()
    (sdk / "system_top.xsa").write_bytes(b"xsa")
    (proj / "system_top_timing_summary_final.rpt").write_text(FINAL_TIMING)
    (proj / "system_top_bus_skew_final.rpt").write_text("Slack (MET)\n")
    (proj / "hopcap_adrv9361_post_route.rpt").write_text(GATE_REPORT + "\n")
    vtop = ROOT / "third_party/vrt49/hdl" / aw.A9_TOP
    (proj / "system_top.v").write_text(aw.patch_a9_system_top(vtop.read_text()))
    (ws / "corundum").mkdir()
    import verify_evidence as ve

    (ws / "MANIFEST.json").write_text(
        json.dumps(
            {
                "schema": "hopcap-workspace-manifest/v1",
                "profile": "adrv9361",
                "physical_port": "sfp",
                "build_env": {},
                "pins": PINS,
                "patch_queue": [],
                "contract_checks": [],
                "adi_hdl_head": PINS["adi_hdl"]["revision"],
                "corundum_head": PINS["corundum"]["revision"],
                "files": {
                    rel: "0" * 64
                    for rel in (
                        *ve.A9_MANIFEST_FILES,
                        "library/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v",
                    )
                },
            }
        )
    )
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "vivado").write_text("#!/bin/sh\necho 'vivado v2025.1 (64-bit)'\n")
    # bootgen -image <bif> -arch zynq -process_bitstream bin -w on: writes <bit>.bin
    (stub / "bootgen").write_text(
        "#!/bin/sh\n"
        'echo "$@" > "$(dirname "$2")/bootgen.args"\n'
        "bit=$(sed -n 3p \"$2\" | tr -d ' ')\n"
        'cp "$bit" "$bit.bin"\n'
    )
    for f in ("vivado", "bootgen"):
        (stub / f).chmod(0o755)
    settings = tmp_path / "settings64.sh"
    settings.write_text(f'export PATH="{stub}:$PATH"\n')
    return ws, settings, tmp_path / "evidence"


def _run(ws, settings, evidence):
    env = dict(
        os.environ,
        ADI_HDL_SHA=PINS["adi_hdl"]["revision"],
        CORUNDUM_SHA=PINS["corundum"]["revision"],
        HOPCAP_SOURCE_SHA="c" * 40,
    )
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--post-only",
            "--ws",
            str(ws),
            "--vivado-settings",
            str(settings),
            "--evidence",
            str(evidence),
        ],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash missing")
def test_adrv9361_post_build_produces_bit_and_fpga_manager_bin(ws):
    r = _run(*ws)
    assert r.returncode == 0, r.stdout + r.stderr
    evidence = ws[2]
    for f in ("system_top.bit", "system_top.bin", "system_top.xsa", "SHA256SUMS", "MANIFEST.json"):
        assert (evidence / f).exists(), f
    args = (evidence / "bootgen.args").read_text()
    assert "-arch zynq -process_bitstream bin" in args
    sums = (evidence / "SHA256SUMS").read_text()
    assert "system_top.bin" in sums and "system_top.bit" in sums
    summary = (evidence / "summary.txt").read_text()
    assert "profile=adrv9361" in summary and "fpga_manager_bin_sha256=" in summary
    assert "check_hop_netlist: profile adrv9361: OK" in summary
    assert "check-sfp-rx-netlist: SKIPPED" in summary


def test_adrv9361_post_build_fails_on_gate_report_or_timing(ws):
    ws_dir, settings, evidence = ws
    proj = ws_dir / "hdl/projects/adrv9361z7035_vrt49_sfp"
    (proj / "hopcap_adrv9361_post_route.rpt").write_text("HOPCAP_ADRV9361 FAILED\n")
    r = _run(ws_dir, settings, evidence)
    assert r.returncode != 0
    assert "ALL PASS" in r.stdout + r.stderr
    (proj / "hopcap_adrv9361_post_route.rpt").unlink()
    r = _run(ws_dir, settings, evidence)
    assert r.returncode != 0
    (proj / "hopcap_adrv9361_post_route.rpt").write_text("HOPCAP_ADRV9361 ALL PASS\n")
    (proj / "system_top_timing_summary_final.rpt").write_text(
        FINAL_TIMING.replace("All user specified timing constraints are met.", "Timing VIOLATED")
    )
    r = _run(ws_dir, settings, evidence)
    assert r.returncode != 0 and "final routed timing not met" in r.stdout + r.stderr


def test_adrv9361_refuses_a_nonempty_missing_external_marker(ws):
    ws_dir, settings, evidence = ws
    (ws_dir / "hdl/library").mkdir(parents=True, exist_ok=True)
    (ws_dir / "hdl/library/missing_external.log").write_text("eth_mac_1g.v\n")
    r = _run(ws_dir, settings, evidence)
    assert r.returncode != 0 and "missing_external.log" in r.stderr


def test_adrv9361_evidence_passes_verify_evidence_and_fails_closed(ws):
    import verify_evidence as ve

    r = _run(*ws)
    assert r.returncode == 0, r.stdout + r.stderr
    evidence = ws[2]
    assert ve.verify_bitstream(evidence) == []
    # a .bin that is not the one recorded fails
    (evidence / "system_top.bin").write_bytes(b"other")
    errs = ve.verify_bitstream(evidence)
    assert any("system_top.bin sha256 mismatch" in e for e in errs)
    assert any("fpga_manager_bin_sha256" in e for e in errs)
    # the adrv9361 profile does not inherit the ZU11EG timing / RX-netlist lines
    summary = (evidence / "summary.txt").read_text()
    (evidence / "summary.txt").write_text(summary.replace("profile adrv9361: OK", "OK"))
    assert any("adrv9361 profile OK" in e for e in ve.verify_bitstream(evidence))
