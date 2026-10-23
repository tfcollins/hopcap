# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import check_hop_netlist as chn  # noqa: E402


def _tree(tmp_path, netlist: str, top: str):
    proj = tmp_path / "projects/adrv9009zu11eg_vrt49_corundum"
    (proj / "synth").mkdir(parents=True)
    (proj / "synth/system.v").write_text(netlist)
    topf = tmp_path / "projects/adrv9009zu11eg/adrv2crr_fmc/system_top_corundum.v"
    topf.parent.mkdir(parents=True)
    topf.write_text(top)
    return proj, topf


GOOD_NET = "module system; vrt49_fhm_hop_ctrl u0(); axi_timed_command_scheduler u1(); axi_vrt49_framer u2(); wire fabric_rf_frequency_q44_20; wire fabric_context_accepted; endmodule"
GOOD_TOP = "ad_iobuf #(.DATA_WIDTH(60)) x();\n  assign adrv9009_gpio_00_a = tick_fhm_gpio[0];\n  system_wrapper i_system_wrapper (\n    .tick_fhm_gpio (tick_fhm_gpio),\n"


def test_passes_on_complete_tree(tmp_path):
    proj, top = _tree(tmp_path, GOOD_NET, GOOD_TOP)
    assert chn.check(proj, top) == []


def test_matches_vivado_bd_instance_names(tmp_path):
    bd = ("module system; system_vrt49_fhm_hop_ctrl_0_0 u0(); system_axi_timed_command_scheduler_0_0 u1(); "
          "system_axi_vrt49_framer_0_0 u2(); wire vrt49_fhm_hop_ctrl_0_fabric_rf_frequency_q44_20; "
          "wire axi_vrt49_framer_0_fabric_context_accepted; endmodule")
    proj, top = _tree(tmp_path, bd, GOOD_TOP)
    assert chn.check(proj, top) == []


def test_fails_when_controller_missing_or_top_unpatched(tmp_path):
    proj, top = _tree(
        tmp_path, GOOD_NET.replace("vrt49_fhm_hop_ctrl u0();", ""), GOOD_TOP.replace("(60)", "(61)")
    )
    errs = chn.check(proj, top)
    assert any("vrt49_fhm_hop_ctrl" in e for e in errs) and any("DATA_WIDTH(60)" in e for e in errs)
