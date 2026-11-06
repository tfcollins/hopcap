# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import check_hop_netlist as chn  # noqa: E402
import pytest  # noqa: E402


def _tree(tmp_path, netlist: str, top: str):
    proj = tmp_path / "projects/adrv9009zu11eg_vrt49_corundum"
    (proj / "synth").mkdir(parents=True)
    (proj / "synth/system.v").write_text(netlist)
    topf = tmp_path / "projects/adrv9009zu11eg/adrv2crr_fmc/system_top_corundum.v"
    topf.parent.mkdir(parents=True)
    topf.write_text(top)
    return proj, topf


# The PPS path as Vivado writes it into the BD's synth/system.v: instances of the top module
# with one .port(net) per line (xlslice as an instance, Corundum behind a hierarchy pin).
PPS_BLOCK = """
  wire corundum_hierarchy_ptp_pps_str;
  wire [0:0]vrt49_external_pps_slice_Dout;
  wire tick_pps_select_0_pps_out;
  system_axi_timed_command_scheduler_0_0 axi_timed_command_scheduler_0
       (.command_irq(axi_timed_command_scheduler_0_command_irq),
        .pps_in(tick_pps_select_0_pps_out),
        .s_axi_aclk(sys_cpu_clk));
  corundum_hierarchy_imp_1RF6VQM corundum_hierarchy
       (.ptp_pps_str(corundum_hierarchy_ptp_pps_str),
        .ptp_sync_pps_str(corundum_hierarchy_ptp_sync_pps_str));
  system_vrt49_external_pps_slice_0 vrt49_external_pps_slice
       (.Din(gpio_i_1),
        .Dout(vrt49_external_pps_slice_Dout));
  system_vrt49_time_base_0_0 vrt49_time_base_0
       (.pps_alt_in(vrt49_external_pps_slice_Dout),
        .pps_in(corundum_hierarchy_ptp_pps_str));
  system_tick_pps_select_0_0 tick_pps_select_0
       (.clk(sys_cpu_clk),
        .ext_alive(tick_pps_select_0_ext_alive),
        .ext_pps_async(vrt49_external_pps_slice_Dout),
        .int_pps_async(corundum_hierarchy_ptp_pps_str),
        .pps_out(tick_pps_select_0_pps_out),
        .resetn(sys_cpu_resetn),
        .sel_ext(tick_pps_select_0_sel_ext));
"""
GOOD_NET = (
    "module system\n   (gpio_i);\n  input [94:0]gpio_i;\n"
    "  vrt49_fhm_hop_ctrl u0(); axi_vrt49_framer u2(); wire fabric_rf_frequency_q44_20;"
    " wire fabric_context_accepted;" + PPS_BLOCK + "endmodule\n"
)
GOOD_TOP = "ad_iobuf #(.DATA_WIDTH(60)) x();\n  assign adrv9009_gpio_00_a = tick_fhm_gpio[0];\n  system_wrapper i_system_wrapper (\n    .tick_fhm_gpio (tick_fhm_gpio),\n"


def test_passes_on_complete_tree(tmp_path):
    proj, top = _tree(tmp_path, GOOD_NET, GOOD_TOP)
    assert chn.check(proj, top) == []


def test_matches_vivado_bd_instance_names(tmp_path):
    bd = (
        "module system; system_vrt49_fhm_hop_ctrl_0_0 u0(); "
        "system_axi_vrt49_framer_0_0 u2(); wire vrt49_fhm_hop_ctrl_0_fabric_rf_frequency_q44_20; "
        "wire axi_vrt49_framer_0_fabric_context_accepted;" + PPS_BLOCK + "endmodule"
    )
    proj, top = _tree(tmp_path, bd, GOOD_TOP)
    assert chn.check(proj, top) == []


def test_fails_when_controller_missing_or_top_unpatched(tmp_path):
    proj, top = _tree(
        tmp_path, GOOD_NET.replace("vrt49_fhm_hop_ctrl u0();", ""), GOOD_TOP.replace("(60)", "(61)")
    )
    errs = chn.check(proj, top)
    assert any("vrt49_fhm_hop_ctrl" in e for e in errs) and any("DATA_WIDTH(60)" in e for e in errs)


def test_port_tops_match_the_assembler():
    """The netlist check must read the same top-level the assembler patched for each port."""
    import assemble_workspace as aw

    assert {k: str(v) for k, v in aw.PORT_TOP.items()} == chn.PORT_TOP


@pytest.mark.parametrize("port", ["sfp", "qsfp"])
def test_cli_checks_the_ports_top(tmp_path, port):
    import subprocess

    proj = tmp_path / "projects/adrv9009zu11eg_vrt49_corundum"
    (proj / "synth").mkdir(parents=True)
    (proj / "synth/system.v").write_text(GOOD_NET)
    for p, top in chn.PORT_TOP.items():
        (tmp_path / top).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / top).write_text(GOOD_TOP if p == port else "module system_top; endmodule")
    script = Path(chn.__file__)
    ok = subprocess.run([sys.executable, str(script), str(tmp_path), "--port", port])
    other = "qsfp" if port == "sfp" else "sfp"
    bad = subprocess.run([sys.executable, str(script), str(tmp_path), "--port", other])
    assert ok.returncode == 0 and bad.returncode == 1


# ---------------------------------------------------------------- Tick pps_in selector


def test_pps_path_passes_and_tolerates_a_suffixed_hierarchy_pin():
    assert chn.check_pps(GOOD_NET) == []
    suffixed = GOOD_NET.replace(".ptp_pps_str(", ".ptp_pps_str1(")
    assert chn.check_pps(suffixed) == []


def test_pps_in_on_ticks_constant_tieoff_fails():
    """The image before this change: Tick's pps_in tied to tick_pps_tieoff (xlconstant 0)."""
    net = GOOD_NET.replace(
        ".pps_in(tick_pps_select_0_pps_out)", ".pps_in(tick_pps_tieoff_dout)"
    ).replace(
        "endmodule",
        "  system_tick_pps_tieoff_0 tick_pps_tieoff\n       (.dout(tick_pps_tieoff_dout));\nendmodule",
    )
    errs = chn.check_pps(net)
    assert any("constant" in e and "tick_pps_tieoff_dout" in e for e in errs), errs


@pytest.mark.parametrize("literal", ["1'b0", "1'h0", "0"])
def test_pps_in_on_a_literal_fails(literal):
    net = GOOD_NET.replace(".pps_in(tick_pps_select_0_pps_out)", f".pps_in({literal})")
    assert any("constant" in e for e in chn.check_pps(net))


def test_pps_in_from_somewhere_else_fails():
    net = GOOD_NET.replace(
        ".pps_in(tick_pps_select_0_pps_out),\n        .s_axi",
        ".pps_in(vrt49_external_pps_slice_Dout),\n        .s_axi",
    )
    errs = chn.check_pps(net)
    assert any("is not tick_pps_select_0.pps_out" in e for e in errs), errs


def test_int_input_on_the_external_slice_fails():
    net = GOOD_NET.replace(
        ".int_pps_async(corundum_hierarchy_ptp_pps_str)",
        ".int_pps_async(vrt49_external_pps_slice_Dout)",
    )
    errs = chn.check_pps(net)
    assert any("does not trace to Corundum's ptp_pps_str" in e for e in errs), errs


def test_int_input_on_corundums_sync_pps_fails():
    """ptp_sync_pps_str is Corundum's core-clock copy, not the PTP PPS vrt49's time base uses."""
    net = GOOD_NET.replace(
        ".int_pps_async(corundum_hierarchy_ptp_pps_str)",
        ".int_pps_async(corundum_hierarchy_ptp_sync_pps_str)",
    )
    errs = chn.check_pps(net)
    assert any("does not trace to Corundum's ptp_pps_str" in e for e in errs), errs


def test_ext_input_not_from_the_vrt49_slice_fails():
    net = GOOD_NET.replace(
        ".ext_pps_async(vrt49_external_pps_slice_Dout)", ".ext_pps_async(GND_1_dout)"
    )
    assert any("ext_pps_async" in e for e in chn.check_pps(net))


def test_missing_selector_fails_and_is_reported_by_check(tmp_path):
    net = GOOD_NET.replace("system_tick_pps_select_0_0 tick_pps_select_0", "system_other_0 other_0")
    assert any("tick_pps_select" in e for e in chn.check_pps(net))
    proj, top = _tree(tmp_path, net.replace("tick_pps_select", "x"), GOOD_TOP)
    errs = chn.check(proj, top)
    assert any("lacks tick_pps_select" in e for e in errs)
    assert any("expected one tick_pps_select instance" in e for e in errs)


def test_only_the_top_module_is_parsed():
    """A same-named port inside another module (e.g. the Corundum hierarchy's internals) must
    not satisfy the check."""
    inner = (
        "module corundum_hierarchy_imp_1RF6VQM\n   (ptp_pps_str);\n"
        "  system_corundum_core_0 corundum_core\n       (.ptp_pps_str(x_int));\nendmodule\n"
    )
    net = inner + GOOD_NET.replace(
        ".int_pps_async(corundum_hierarchy_ptp_pps_str)", ".int_pps_async(x_int)"
    )
    assert any("does not trace" in e for e in chn.check_pps(net))


def test_int_input_not_shared_with_the_vrt_time_base_fails():
    net = GOOD_NET.replace(".pps_in(corundum_hierarchy_ptp_pps_str));", ".pps_in(some_other_pps));")
    errs = chn.check_pps(net)
    assert any("is not vrt49_time_base_0.pps_in" in e for e in errs), errs


def test_ext_input_accepted_via_time_base_when_the_slice_is_inlined():
    """If Vivado inlines xlslice (no instance), the time base's pps_alt_in still identifies it."""
    net = GOOD_NET.replace(
        "  system_vrt49_external_pps_slice_0 vrt49_external_pps_slice\n"
        "       (.Din(gpio_i_1),\n"
        "        .Dout(vrt49_external_pps_slice_Dout));\n",
        "  assign vrt49_external_pps_slice_Dout = gpio_i_1[16];\n",
    )
    assert "vrt49_external_pps_slice\n" not in net
    assert chn.check_pps(net) == []
