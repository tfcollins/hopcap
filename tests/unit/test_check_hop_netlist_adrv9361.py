# SPDX-License-Identifier: MIT
"""check_hop_netlist.py --profile adrv9361 on a synthetic project tree written in the syntax
Vivado 2025.1 emits (taken from tick's Z7035 build: BD synth/system.v instances with one
.port(net) per line, xlconcat inlined as an assign, wrapper headers with the IP VLNV and the
wrapped module's #( ... ) inst ( parameter list)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "third_party/tick/scripts"))
import assemble_workspace as aw  # noqa: E402
import check_hop_netlist as chn  # noqa: E402

NETLIST = """
module system
   (tick_clk,
    tick_ctrl_in);
  output tick_clk;
  output [2:0]tick_ctrl_in;
  wire [3:0]axi_timed_command_scheduler_0_gpio_out;
  wire [3:0]tick_hop_gpio_concat_dout;
  wire [0:0]tick_hop_event_slice_Dout;
  wire [2:0]tick_hop_slot_slice_Dout;
  assign tick_hop_gpio_concat_dout = {tick_hop_slot_slice_Dout, tick_hop_event_slice_Dout};
  system_axi_timed_command_scheduler_0_0 axi_timed_command_scheduler_0
       (.command_irq(axi_timed_command_scheduler_0_command_irq),
        .gpio_out(axi_timed_command_scheduler_0_gpio_out),
        .pps_in(tick_pps_gen_0_pps_out),
        .s_axi_aclk(tick_clk),
        .s_axi_aresetn(tick_timebase_reset_peripheral_aresetn));
  system_tick_ctrl_in_slice_0 tick_ctrl_in_slice
       (.Din(axi_timed_command_scheduler_0_gpio_out),
        .Dout(tick_ctrl_in));
  system_tick_hop_event_slice_0 tick_hop_event_slice
       (.Din(axi_timed_command_scheduler_0_gpio_out),
        .Dout(tick_hop_event_slice_Dout));
  system_tick_hop_slot_slice_0 tick_hop_slot_slice
       (.Din(axi_timed_command_scheduler_0_gpio_out),
        .Dout(tick_hop_slot_slice_Dout));
  system_tick_hop_gpio_sync_0 tick_hop_gpio_sync
       (.clk(sys_cpu_clk),
        .gpio_async(tick_hop_gpio_concat_dout),
        .gpio_sync(tick_hop_gpio_sync_gpio_sync));
  system_vrt49_fhm_hop_ctrl_0_0 vrt49_fhm_hop_ctrl_0
       (.clk(sys_cpu_clk),
        .fabric_context_accepted(axi_vrt49_framer_0_fabric_context_accepted),
        .fabric_context_ready(axi_vrt49_framer_0_fabric_context_ready),
        .fabric_context_valid(vrt49_fhm_hop_ctrl_0_fabric_context_valid),
        .fabric_hop_epoch(vrt49_fhm_hop_ctrl_0_fabric_hop_epoch),
        .fabric_rf_frequency_q44_20(vrt49_fhm_hop_ctrl_0_fabric_rf_frequency_q44_20),
        .fabric_samples_drained(axi_vrt49_framer_0_fabric_samples_drained),
        .resetn(sys_cpu_resetn),
        .scheduler_gpio(tick_hop_gpio_sync_gpio_sync),
        .stream_enable(vrt49_fhm_hop_ctrl_0_stream_enable));
  system_axi_vrt49_framer_0_0 axi_vrt49_framer_0
       (.fabric_context_accepted(axi_vrt49_framer_0_fabric_context_accepted),
        .fabric_context_ready(axi_vrt49_framer_0_fabric_context_ready),
        .fabric_context_valid(vrt49_fhm_hop_ctrl_0_fabric_context_valid),
        .fabric_hop_epoch(vrt49_fhm_hop_ctrl_0_fabric_hop_epoch),
        .fabric_rf_frequency_q44_20(vrt49_fhm_hop_ctrl_0_fabric_rf_frequency_q44_20),
        .fabric_sample_enable(vrt49_fhm_hop_ctrl_0_stream_enable),
        .fabric_samples_drained(axi_vrt49_framer_0_fabric_samples_drained),
        .tb_nominal_sps(axi_vrt49_framer_0_tb_nominal_sps),
        .tb_pps_margin(axi_vrt49_framer_0_tb_pps_margin),
        .tb_pps_source(axi_vrt49_framer_0_tb_pps_source),
        .tb_seconds_immediate(axi_vrt49_framer_0_tb_seconds_immediate),
        .tb_seconds_next(axi_vrt49_framer_0_tb_seconds_next),
        .tb_seconds_req(axi_vrt49_framer_0_tb_seconds_req),
        .tb_tuple_count(vrt49_time_base_0_tuple_count),
        .tb_tuple_lock_state(vrt49_time_base_0_tuple_lock_state),
        .tb_tuple_missed_count(vrt49_time_base_0_tuple_missed_count),
        .tb_tuple_pps_period(vrt49_time_base_0_tuple_pps_period),
        .tb_tuple_seconds(vrt49_time_base_0_tuple_seconds),
        .tb_tuple_toggle(vrt49_time_base_0_tuple_toggle));
  system_tick_pps_gen_0_0 tick_pps_gen_0
       (.clk(tick_clk),
        .pps_out(tick_pps_gen_0_pps_out),
        .resetn(tick_timebase_reset_peripheral_aresetn));
  system_hopcap_tb_pps_alt_tieoff_0 hopcap_tb_pps_alt_tieoff
       (.dout(hopcap_tb_pps_alt_tieoff_dout));
  system_vrt49_rx_tap_0 vrt49_rx_tap
       (.clk(util_ad9361_divclk_clk_out),
        .resetn(util_ad9361_divclk_reset_peripheral_aresetn),
        .sample_valid(util_ad9361_adc_pack_packed_fifo_wr_en));
  system_vrt49_time_base_0_0 vrt49_time_base_0
       (.cfg_nominal_sps(axi_vrt49_framer_0_tb_nominal_sps),
        .cfg_pps_margin(axi_vrt49_framer_0_tb_pps_margin),
        .cfg_pps_source(axi_vrt49_framer_0_tb_pps_source),
        .cfg_seconds_immediate(axi_vrt49_framer_0_tb_seconds_immediate),
        .cfg_seconds_next(axi_vrt49_framer_0_tb_seconds_next),
        .cfg_seconds_req(axi_vrt49_framer_0_tb_seconds_req),
        .divclk(util_ad9361_divclk_clk_out),
        .divclk_resetn(util_ad9361_divclk_reset_peripheral_aresetn),
        .pps_alt_in(hopcap_tb_pps_alt_tieoff_dout),
        .pps_in(tick_pps_gen_0_pps_out),
        .sample_valid(util_ad9361_adc_pack_packed_fifo_wr_en),
        .tuple_count(vrt49_time_base_0_tuple_count),
        .tuple_lock_state(vrt49_time_base_0_tuple_lock_state),
        .tuple_missed_count(vrt49_time_base_0_tuple_missed_count),
        .tuple_pps_period(vrt49_time_base_0_tuple_pps_period),
        .tuple_seconds(vrt49_time_base_0_tuple_seconds),
        .tuple_toggle(vrt49_time_base_0_tuple_toggle));
endmodule
"""


def _wrapper(cell: str, module: str, params: dict[str, str], lib: str = "ip") -> str:
    plist = ",\n".join(f"    .{k}({v})" for k, v in params.items())
    body = f"  {module} #(\n{plist}\n  ) inst (\n" if params else f"  {module} inst (\n"
    return (
        f"// IP VLNV: xilinx.com:{lib}:{module}:1.0\n"
        f"module system_{cell}_0 (clk);\n{body}    .clk(clk)\n  );\nendmodule\n"
    )


WRAPPERS = {
    "axi_timed_command_scheduler_0": _wrapper(
        "axi_timed_command_scheduler_0",
        "axi_timed_command_scheduler",
        {"QUEUE_DEPTH": "16", "GPIO_WIDTH": "4", "TIME_TICK_HZ": "50000000"},
        "module_ref",
    ),
    "tick_hop_gpio_sync": _wrapper("tick_hop_gpio_sync", "tick_gpio_sync", {}, "module_ref"),
    "vrt49_link_throttle": _wrapper(
        "vrt49_link_throttle", "vrt49_frame_throttle", {"DATA_WIDTH": "32", "FORWARD_EVERY_N": "1"}
    ),
    "tick_pps_gen_0": _wrapper(
        "tick_pps_gen_0", "tick_pps_gen", {"TICK_HZ": "50000000", "PULSE_TICKS": "5000000"}
    ),
    "tick_hop_event_slice": _wrapper(
        "tick_hop_event_slice",
        "xlslice_v1_0_5_xlslice",
        {"DIN_WIDTH": "4", "DIN_FROM": "3", "DIN_TO": "3"},
    ),
    "tick_hop_slot_slice": _wrapper(
        "tick_hop_slot_slice",
        "xlslice_v1_0_5_xlslice",
        {"DIN_WIDTH": "4", "DIN_FROM": "2", "DIN_TO": "0"},
    ),
    "tick_ctrl_in_slice": _wrapper(
        "tick_ctrl_in_slice",
        "xlslice_v1_0_5_xlslice",
        {"DIN_WIDTH": "4", "DIN_FROM": "2", "DIN_TO": "0"},
    ),
}

GATE_REPORT = "\n".join(
    [
        "HOPCAP_ADRV9361 PASS tick_hop_gpio_sync (tick_clk -> sys_cpu_clk): false path into x/D",
        "HOPCAP_ADRV9361 PASS CTRL_IN IOB i_system_top/tick_ctrl_in_iob_reg[0] LOC=OLOGIC_X0Y86 BEL=OLOGICE2.OUTFF",
        "HOPCAP_ADRV9361 PASS CTRL_IN IOB i_system_top/tick_ctrl_in_iob_reg[1] LOC=OLOGIC_X0Y85 BEL=OLOGICE2.OUTFF",
        "HOPCAP_ADRV9361 PASS CTRL_IN IOB i_system_top/tick_ctrl_in_iob_reg[2] LOC=OLOGIC_X0Y88 BEL=OLOGICE2.OUTFF",
        "HOPCAP_ADRV9361 ALL PASS",
    ]
)


@pytest.fixture
def a9_tree(tmp_path):
    """A routed adrv9361 project as check_hop_netlist sees it: BD netlist, wrappers, gate
    report, and the assembler-patched top from the real pinned vrt49 source."""
    hdl = tmp_path / "hdl"
    proj = hdl / chn.A9_PROJECT
    gen = proj / "adrv9361z7035_vrt49_sfp.gen/sources_1/bd/system"
    (gen / "synth").mkdir(parents=True)
    (gen / "synth/system.v").write_text(NETLIST)
    for cell, text in WRAPPERS.items():
        d = gen / "ip" / f"system_{cell}_0" / "synth"
        d.mkdir(parents=True)
        (d / f"system_{cell}_0.v").write_text(text)
    (proj / chn.A9_GATE_REPORT).write_text(GATE_REPORT + "\n")
    vtop = ROOT / "third_party/vrt49/hdl" / aw.A9_TOP
    (hdl / chn.A9_TOP).write_text(aw.patch_a9_system_top(vtop.read_text()))
    return hdl, proj, hdl / chn.A9_TOP


def test_a9_paths_agree_with_the_assembler():
    assert aw.A9_PROJECT_REL.as_posix() == chn.A9_PROJECT
    assert aw.A9_TOP.as_posix() == chn.A9_TOP


def test_a9_passes_on_the_complete_tree(a9_tree):
    _, proj, top = a9_tree
    assert chn.check(proj, top, "adrv9361") == []


def test_a9_cli(a9_tree):
    hdl, _, _ = a9_tree
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_hop_netlist.py"),
            str(hdl),
            "--port",
            "sfp",
            "--profile",
            "adrv9361",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "profile adrv9361: OK" in r.stdout
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_hop_netlist.py"),
            str(hdl),
            "--profile",
            "adrv9361",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and "--port sfp" in r.stderr


def _rewrite(proj: Path, old: str, new: str) -> None:
    f = next(proj.glob("**/synth/system.v"))
    text = f.read_text()
    assert old in text, old
    f.write_text(text.replace(old, new))


def test_a9_fails_when_hop1_or_vrt49_tick_is_back(a9_tree):
    _, proj, top = a9_tree
    _rewrite(
        proj,
        "endmodule",
        "  system_vrt49_hop_ctrl_0_0 vrt49_hop_ctrl_0\n"
        "       (.clk(sys_cpu_clk));\n  vrt49_ad9361_hop_ctrl x();\nendmodule",
    )
    errs = chn.check(proj, top, "adrv9361")
    assert any("vrt49_ad9361_hop_ctrl" in e for e in errs)


def test_a9_fails_on_swapped_event_and_slot(a9_tree):
    _, proj, top = a9_tree
    _rewrite(
        proj,
        "{tick_hop_slot_slice_Dout, tick_hop_event_slice_Dout}",
        "{tick_hop_event_slice_Dout, tick_hop_slot_slice_Dout}",
    )
    errs = chn.check(proj, top, "adrv9361")
    assert any("slot bits 3:1, event bit 0" in e for e in errs), errs


def test_a9_fails_when_hop2_bypasses_the_sync(a9_tree):
    _, proj, top = a9_tree
    _rewrite(
        proj,
        ".scheduler_gpio(tick_hop_gpio_sync_gpio_sync)",
        ".scheduler_gpio(axi_timed_command_scheduler_0_gpio_out)",
    )
    errs = chn.check(proj, top, "adrv9361")
    assert any("HOP2 GPIO" in e for e in errs), errs


def test_a9_fails_when_tick_pps_is_tied_off_or_time_base_misclocked(a9_tree):
    _, proj, top = a9_tree
    _rewrite(
        proj,
        ".pps_in(tick_pps_gen_0_pps_out),\n        .s_axi_aclk",
        ".pps_in(1'b0),\n        .s_axi_aclk",
    )
    _rewrite(proj, ".divclk(util_ad9361_divclk_clk_out)", ".divclk(sys_cpu_clk)")
    errs = chn.check(proj, top, "adrv9361")
    assert any("pps_in" in e for e in errs) and any("tap's clock" in e for e in errs), errs


def test_a9_fails_on_throttle_tick_rate_and_non_module_ref_tick(a9_tree):
    _, proj, top = a9_tree
    for cell, old, new in (
        ("vrt49_link_throttle", ".FORWARD_EVERY_N(1)", ".FORWARD_EVERY_N(3)"),
        ("axi_timed_command_scheduler_0", ".TIME_TICK_HZ(50000000)", ".TIME_TICK_HZ(100000000)"),
        ("axi_timed_command_scheduler_0", "xilinx.com:module_ref:", "analog.com:user:"),
    ):
        f = next(proj.glob(f"**/ip/system_{cell}_0/synth/system_{cell}_0.v"))
        f.write_text(f.read_text().replace(old, new))
    errs = chn.check(proj, top, "adrv9361")
    assert any("FORWARD_EVERY_N = 3" in e for e in errs)
    assert any("TIME_TICK_HZ = 100000000" in e for e in errs)
    assert any("not a module reference" in e for e in errs)


def test_a9_requires_the_post_route_gate_report(a9_tree):
    _, proj, top = a9_tree
    rpt = proj / chn.A9_GATE_REPORT
    rpt.write_text(
        GATE_REPORT.replace("ALL PASS", "FAILED").replace(
            "PASS CTRL_IN IOB i_system_top/tick_ctrl_in_iob_reg[2] LOC=OLOGIC_X0Y88 BEL=OLOGICE2.OUTFF",
            "FAIL CTRL_IN IOB i_system_top/tick_ctrl_in_iob_reg[2] not in an OLOGIC site (LOC=SLICE_X0Y1 BEL=AFF)",
        )
    )
    errs = chn.check(proj, top, "adrv9361")
    assert any("not in an OLOGIC site" in e for e in errs)
    assert any("ALL PASS" in e for e in errs)
    assert any("2 CTRL_IN IOB PASS lines" in e for e in errs)
    rpt.unlink()
    assert any("post-route gate did not run" in e for e in chn.check(proj, top, "adrv9361"))


def test_a9_fails_on_an_unpatched_top(a9_tree):
    _, proj, top = a9_tree
    top.write_text((ROOT / "third_party/vrt49/hdl" / aw.A9_TOP).read_text())
    errs = chn.check(proj, top, "adrv9361")
    assert any("ad9361_fastlock_select" in e for e in errs)
    assert any("tick_ctrl_in_iob" in e for e in errs)
