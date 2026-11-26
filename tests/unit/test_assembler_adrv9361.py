# SPDX-License-Identifier: MIT
"""assemble_workspace.py --profile adrv9361: the ADRV9361-Z7035 hop composite on vrt49's
adrv9361z7035_vrt49_sfp.  The four patched files are vrt49 sources, so every patch is applied
to the real pinned submodule copies; the fragment and CDC scripts are checked against the real
RTL register names they constrain."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "third_party/tick/scripts"))
import assemble_workspace as aw  # noqa: E402
import prepare_adi_hdl_adrv9361z7035 as tick_a9  # noqa: E402

VRT49 = ROOT / "third_party/vrt49"
TICK = ROOT / "third_party/tick"
VPROJ = VRT49 / "hdl" / aw.A9_PROJECT_REL
FRAGMENT = (ROOT / "hdl/system_bd_adrv9361_hop.tcl").read_text()
CDC = (ROOT / "hdl/system_adrv9361_hop_cdc.tcl").read_text()
GATE = (ROOT / "hdl/check_adrv9361_hop.tcl").read_text()
TICK_FRAGMENT = (TICK / "hdl" / aw.A9_TICK_PROJECT_REL / aw.A9_TICK_FRAGMENT).read_text()
TICK_XDC = (TICK / "hdl" / aw.A9_TICK_PROJECT_REL / aw.A9_TICK_XDC).read_text()


def _src(name: str) -> str:
    return (VPROJ / name).read_text()


def _code(tcl: str) -> str:
    """Tcl without comment lines."""
    return "\n".join(ln for ln in tcl.splitlines() if not ln.lstrip().startswith("#"))


# ---------------------------------------------------------------------------------------
# patches on the real pinned vrt49 sources


def test_system_bd_patch_cuts_vrt49_tick_and_hop1_and_sources_the_fragment():
    orig = _src("system_bd.tcl")
    new = aw.patch_a9_system_bd(orig)
    code = _code(new)
    for gone in (
        "tick_scheduler_0",
        "vrt49_hop_ctrl_0",
        "ad9361_fastlock_select",
        "vrt49_ad9361_hop_ctrl",
        "ad_ip_instance axi_timed_command_scheduler",
    ):
        assert gone not in code, gone
    lines = [ln.strip() for ln in new.splitlines()]
    assert lines.count("validate_bd_design") == 1
    i_src = lines.index(aw.A9_HOP_FRAGMENT_SOURCE)
    assert lines[i_src + 1] == "validate_bd_design"
    # everything else of vrt49's body is untouched
    for keep in (
        "ad_ip_instance vrt49_frame_throttle vrt49_link_throttle",
        "ad_cpu_interconnect 0x44A80000 axi_vrt49_framer_0",
        "ad_cpu_interconnect 0x44A90000 vrt49_udp_tx_0",
        "ad_connect util_ad9361_adc_pack/packed_fifo_wr_en vrt49_rx_tap/sample_valid",
        "ad_connect vrt49_zero_1/dout axi_vrt49_framer_0/tb_tuple_toggle",
    ):
        assert keep in new, keep
    assert len(orig.splitlines()) - len(new.splitlines()) > 20  # only the Tick + HOP1 block
    assert aw.patch_a9_system_bd(new) == new


def test_system_bd_patch_refuses_a_changed_block():
    orig = _src("system_bd.tcl")
    with pytest.raises(aw.AssembleError, match="anchors"):
        aw.patch_a9_system_bd(orig.replace(aw.A9_BD_CUT_START, "# moved\n"))
    # an extra cell inside the cut would be silently deleted: refuse
    extra = orig.replace(aw.A9_BD_CUT_END, aw.A9_BD_CUT_END + "", 1).replace(
        "ad_cpu_interconnect 0x44AB0000 vrt49_hop_ctrl_0\n",
        "ad_cpu_interconnect 0x44AB0000 vrt49_hop_ctrl_0\nad_ip_instance xlconstant other_0\n",
    )
    with pytest.raises(aw.AssembleError, match="creates"):
        aw.patch_a9_system_bd(extra)
    with pytest.raises(aw.AssembleError, match="validate_bd_design"):
        aw.patch_a9_system_bd(
            orig.replace("validate_bd_design", "validate_bd_design\nvalidate_bd_design")
        )


def test_makefile_patch_drops_vendored_tick_and_hop1():
    orig = _src("Makefile")
    new = aw.patch_a9_makefile(orig)
    deps = " ".join(ln for ln in new.splitlines() if ln.startswith("LIB_DEPS"))
    assert "tick_vendor" not in deps and "vrt49_ad9361_hop_ctrl" not in deps
    for need in ("vrt49_time_base", "vrt49_fhm_hop_ctrl", "tick_pps_gen", "axi_vrt49_framer"):
        assert need in deps.split(), need
    for m in (
        "M_DEPS += system_bd_adrv9361_hop.tcl",
        "M_DEPS += system_adrv9361_hop_cdc.tcl",
        "M_DEPS += check_adrv9361_hop.tcl",
        "M_DEPS += ../adrv9361z7035/ccfmc_lvds/tick_adrv9361z7035_ccfmc_lvds.tcl",
        "M_DEPS += ../adrv9361z7035/ccfmc_lvds/tick_adrv9361z7035_ctrl_in.xdc",
        "M_DEPS += ../../library/axi_timed_command_scheduler/axi_timed_command_scheduler.v",
        "M_DEPS += ../../library/tick_gpio_sync/tick_gpio_sync.v",
    ):
        assert m in new, m
    assert new.index("tick_pps_gen") < new.index("include ../scripts/project-xilinx.mk")
    assert aw.patch_a9_makefile(new) == new
    with pytest.raises(aw.AssembleError):
        aw.patch_a9_makefile(orig.replace(aw.A9_LIB_DEPS_OLD, "LIB_DEPS += tick_vendor\n"))


def test_system_project_patch_adds_xdc_cdc_script_and_post_route_gate():
    orig = _src("system_project.tcl")
    new = aw.patch_a9_system_project(orig)
    files_list = new[new.index("adi_project_files") : new.index('"system_top.v" ]')]
    assert aw.A9_PROJECT_XDC_ENTRY in files_list
    assert "system_tick_pps_cdc.xdc" not in new  # ZU11EG-only (pmod0_d4, tick_pps_select)
    # the CDC script: a late, implementation-only constraint, like vrt49's own
    for line in (
        'set hopcap_a9_cdc_tcl [file normalize "system_adrv9361_hop_cdc.tcl"]',
        "set_property USED_IN_SYNTHESIS false [get_files $hopcap_a9_cdc_tcl]",
        "set_property PROCESSING_ORDER LATE [get_files $hopcap_a9_cdc_tcl]",
    ):
        assert line in new, line
    # the gate runs on the final routed checkpoint, after vrt49's timing and bus-skew gates
    gate = new.index("source check_adrv9361_hop.tcl")
    assert new.index("open_run impl_1") < gate
    assert new.index('error "Final routed VRT49 bus-skew constraints are not met"') < gate
    assert new.index("adi_project_run adrv9361z7035_vrt49_sfp") < gate
    assert aw.patch_a9_system_project(new) == new


def test_system_top_patch_drives_ctrl_in_from_tick_iob_registers():
    orig = _src("system_top.v")
    new = aw.patch_a9_system_top(orig)
    assert "ad9361_fastlock_select" not in new
    assert "  wire            tick_clk;\n  wire    [ 2:0]  tick_ctrl_in;\n" in new
    assert "    .tick_clk (tick_clk),\n    .tick_ctrl_in (tick_ctrl_in),\n" in new
    assert '(* IOB = "TRUE" *) reg [2:0] tick_ctrl_in_iob' in new
    assert "  always @(posedge tick_clk) begin\n    tick_ctrl_in_iob <= tick_ctrl_in;" in new
    # The FastLock profile pins are CTRL_IN1..3 (ADI EngineerZone 97390), so Tick drives
    # gpio_ctl[3:1] and CTRL_IN0 moves into vrt49's PS-owned iobuf in place of CTRL_IN3.
    assert ".dio_p (gpio_ctl[3:1]));" in new and ".dio_o (gpio_i[43:41])," in new
    assert "gpio_ctl[0],        // 40:40 / CTRL_IN0 (PS-owned; hopcap)" in new
    assert ".dio_o ({gpio_i[46:44], gpio_i[40], gpio_i[39:32]})," in new
    assert "gpio_ctl[3],        // 43:43" not in new and "gpio_ctl[2:0]" not in new
    assert aw.patch_a9_system_top(new) == new


def test_system_top_iob_block_is_ticks_own():
    """The register + ad_iobuf lines are Tick's SYSTEM_TOP_IOBUF_NEW tail, verbatim."""
    new = aw.patch_a9_system_top(_src("system_top.v"))
    tail = tick_a9.SYSTEM_TOP_IOBUF_NEW[tick_a9.SYSTEM_TOP_IOBUF_NEW.index(aw.A9_TICK_IOB_HEAD) :]
    assert tail in new
    # Tick's XDC packs exactly the register this block declares
    assert "tick_ctrl_in_iob_reg" in TICK_XDC
    assert "set_false_path -to [get_ports {gpio_ctl[1] gpio_ctl[2] gpio_ctl[3]}]" in TICK_XDC


def test_system_top_patch_refuses_changed_anchors():
    orig = _src("system_top.v")
    with pytest.raises(aw.AssembleError, match="layout changed"):
        aw.patch_a9_system_top(orig.replace(aw.A9_TOP_ASSIGN_OLD, ""))
    with pytest.raises(aw.AssembleError, match="layout changed"):
        aw.patch_a9_system_top(orig.replace(aw.A9_TOP_WRAPPER_OLD, ""))


def test_bd_ports_match_the_patched_wrapper_connections():
    """Every port the BD loses or gains is connected exactly so in the patched top."""
    top = aw.patch_a9_system_top(_src("system_top.v"))
    bd_new_ports = set(re.findall(r"^create_bd_port\b.*?\s(\S+)\s*$", TICK_FRAGMENT, re.M))
    bd_new_ports |= set(re.findall(r"^create_bd_port\b.*?\s(\S+)\s*$", FRAGMENT, re.M))
    assert bd_new_ports == {"tick_clk", "tick_ctrl_in"}
    wrapper = top[top.index("system_wrapper i_system_wrapper") :]
    for port in bd_new_ports:
        assert f".{port} ({port})," in wrapper
    assert ".ad9361_fastlock_select" not in wrapper
    # the fragment removes the port vrt49 created (if a body still has it)
    assert "delete_bd_objs [get_bd_ports ad9361_fastlock_select]" in FRAGMENT


# ---------------------------------------------------------------------------------------
# the hop fragment


def _ordered(text: str, *needles: str) -> None:
    pos = [text.index(n) for n in needles]
    assert pos == sorted(pos), needles


def test_fragment_deletes_vrt49_tick_and_hop1_before_sourcing_ticks_fragment():
    code = _code(FRAGMENT)
    _ordered(
        code,
        "foreach cell {tick_scheduler_0 vrt49_hop_ctrl_0}",
        "delete_bd_objs [get_bd_ports ad9361_fastlock_select]",
        "source $ad_hdl_dir/projects/adrv9361z7035/ccfmc_lvds/tick_adrv9361z7035_ccfmc_lvds.tcl",
        "ad_ip_instance vrt49_fhm_hop_ctrl vrt49_fhm_hop_ctrl_0",
    )
    assert "validate_bd_design" not in code  # system_bd.tcl validates right after


def test_tick_fragment_is_at_0x44aa0000_ps14_and_50mhz():
    assert "ad_cpu_interconnect 0x44AA0000 axi_timed_command_scheduler_0 S_AXI" in TICK_FRAGMENT
    assert (
        'ad_cpu_interrupt "ps-14" "mb-14" axi_timed_command_scheduler_0/command_irq'
        in TICK_FRAGMENT
    )
    assert "set tick_time_tick_hz 50000000" in TICK_FRAGMENT
    assert "set hopcap_tick_hz 50000000" in FRAGMENT


def test_ps14_is_free_in_vrt49s_design():
    """ADI's adrv9361z7035/ccfmc BD uses ps-11..13 and ps-15 (Tick fragment header); vrt49's
    SFP body adds no interrupt, and the cut block had none either."""
    assert "ad_cpu_interrupt" not in _src("system_bd.tcl")
    assert "ad_cpu_interrupt" not in FRAGMENT


def test_fragment_hop2_and_gpio_reorder():
    code = _code(FRAGMENT)
    assert "set hop_ctrl_base 0x44AB0000" in code
    assert "ad_cpu_interconnect $hop_ctrl_base vrt49_fhm_hop_ctrl_0" in code
    assert "ad_connect sys_cpu_clk    vrt49_fhm_hop_ctrl_0/clk" in code
    # {gpio_out[2:0], gpio_out[3]}: In0 (LSB) = event bit 3, In1 = slot bits 2:0
    assert re.search(
        r"xlslice tick_hop_event_slice \[list \\\n\s+DIN_WIDTH 4 DIN_FROM 3 DIN_TO 3 DOUT_WIDTH 1\]",
        code,
    )
    assert re.search(
        r"xlslice tick_hop_slot_slice \[list \\\n\s+DIN_WIDTH 4 DIN_FROM 2 DIN_TO 0 DOUT_WIDTH 3\]",
        code,
    )
    assert "NUM_PORTS 2 IN0_WIDTH 1 IN1_WIDTH 3" in code
    assert "ad_connect tick_hop_event_slice/Dout tick_hop_gpio_concat/In0" in code
    assert "ad_connect tick_hop_slot_slice/Dout  tick_hop_gpio_concat/In1" in code
    assert "create_bd_cell -type module -reference tick_gpio_sync tick_hop_gpio_sync" in code
    assert "ad_connect sys_cpu_clk tick_hop_gpio_sync/clk" in code
    assert "ad_connect tick_hop_gpio_concat/dout tick_hop_gpio_sync/gpio_async" in code
    assert "ad_connect tick_hop_gpio_sync/gpio_sync vrt49_fhm_hop_ctrl_0/scheduler_gpio" in code
    # the module reference is in the project before create_bd_cell
    _ordered(
        code,
        "tick_gpio_sync/tick_gpio_sync.v",
        "add_files -norecurse $rtl",
        "-reference tick_gpio_sync",
    )
    # the seven framer fabric nets
    for pin in (
        "fabric_sample_enable",
        "fabric_samples_drained",
        "fabric_context_valid",
        "fabric_context_ready",
        "fabric_rf_frequency_q44_20",
        "fabric_hop_epoch",
        "fabric_context_accepted",
    ):
        assert re.search(
            rf"ad_connect \S*vrt49_fhm_hop_ctrl_0/\S+\s+axi_vrt49_framer_0/{pin}|"
            rf"ad_connect axi_vrt49_framer_0/{pin}\s+vrt49_fhm_hop_ctrl_0/",
            code,
        ), pin


def test_gpio_sync_event_bit_is_bit0():
    """tick_gpio_sync gives bit 0 the extra (event) stage: the reorder must put gpio_out[3]
    there."""
    rtl = (ROOT / "hdl/tick_gpio_sync/tick_gpio_sync.v").read_text()
    assert "assign gpio_sync = {gpio_s1[3:1], event_s2};" in rtl
    assert "event_s2 <= gpio_s1[0];" in rtl


def test_fragment_pps_time_base_and_throttle():
    code = _code(FRAGMENT)
    assert "delete_bd_objs [get_bd_cells tick_pps_tieoff]" in code
    assert "ad_ip_instance tick_pps_gen tick_pps_gen_0" in code
    assert "TICK_HZ $hopcap_tick_hz" in code and "PULSE_TICKS [expr {$hopcap_tick_hz / 10}]" in code
    assert "ad_connect tick_clkgen/clk_out1 tick_pps_gen_0/clk" in code
    assert "ad_connect tick_timebase_reset/peripheral_aresetn tick_pps_gen_0/resetn" in code
    assert "ad_connect tick_pps_gen_0/pps_out axi_timed_command_scheduler_0/pps_in" in code
    assert "ad_connect tick_pps_gen_0/pps_out vrt49_time_base_0/pps_in" in code
    # time base on the tap's clock, counting the tap's beats
    assert "ad_connect util_ad9361_divclk/clk_out vrt49_time_base_0/divclk" in code
    assert (
        "ad_connect util_ad9361_divclk_reset/peripheral_aresetn vrt49_time_base_0/divclk_resetn"
        in code
    )
    assert (
        "ad_connect util_ad9361_adc_pack/packed_fifo_wr_en vrt49_time_base_0/sample_valid" in code
    )
    assert "ad_connect hopcap_tb_pps_alt_tieoff/dout vrt49_time_base_0/pps_alt_in" in code
    for sig in ("toggle", "seconds", "count", "pps_period", "lock_state", "missed_count"):
        assert f"tb_tuple_{sig}" in code
    for out, inp in (
        ("tb_seconds_req", "cfg_seconds_req"),
        ("tb_seconds_next", "cfg_seconds_next"),
        ("tb_seconds_immediate", "cfg_seconds_immediate"),
        ("tb_nominal_sps", "cfg_nominal_sps"),
        ("tb_pps_margin", "cfg_pps_margin"),
        ("tb_pps_source", "cfg_pps_source"),
    ):
        assert re.search(rf"axi_vrt49_framer_0/{out}\s+vrt49_time_base_0/{inp}\b", code), out
    assert "ad_ip_parameter vrt49_link_throttle CONFIG.FORWARD_EVERY_N 1" in code
    # vrt49 ties the tuple inputs to the shared constants this fragment detaches from
    vbd = _src("system_bd.tcl")
    for sig in ("toggle", "seconds", "count", "pps_period", "lock_state", "missed_count"):
        assert re.search(
            rf"ad_connect vrt49_zero_\d+/dout axi_vrt49_framer_0/tb_tuple_{sig}\n", vbd
        )
    assert "FORWARD_EVERY_N 3" in vbd  # the value the fragment overrides


def test_tick_pps_gen_ip_matches_fragment_ports():
    rtl = (ROOT / "hdl/tick_pps_gen/tick_pps_gen.v").read_text()
    for port in ("input wire clk", "input wire resetn", "output reg pps_out"):
        assert port in rtl
    assert "parameter integer TICK_HZ = 50000000" in rtl
    assert "parameter integer PULSE_TICKS = 5000000" in rtl


# ---------------------------------------------------------------------------------------
# CDC constraint script and post-route gate


def _regs(path: Path) -> str:
    return path.read_text()


TB_RTL = _regs(VRT49 / "hdl/vrt49_time_base/vrt49_time_base.v")
FRAMER_RTL = _regs(VRT49 / "hdl/axi_vrt49_framer/axi_vrt49_framer.v")
SYNC_RTL = _regs(ROOT / "hdl/tick_gpio_sync/tick_gpio_sync.v")


@pytest.mark.parametrize(
    ("rtl", "reg"),
    [
        (SYNC_RTL, "gpio_meta"),
        (SYNC_RTL, "gpio_s1"),
        (TB_RTL, "pps_meta"),
        (TB_RTL, "pps_sync"),
        (TB_RTL, "ppssel_meta"),
        (TB_RTL, "ppssel_sync"),
        (TB_RTL, "secreq_meta"),
        (TB_RTL, "secreq_sync"),
        (TB_RTL, "cfg_nominal_sps_meta"),
        (TB_RTL, "cfg_pps_margin_meta"),
        (TB_RTL, "cfg_seconds_immediate_meta"),
        (TB_RTL, "seconds_armed"),
        (TB_RTL, "tuple_toggle"),
        (FRAMER_RTL, "tgl_meta"),
        (FRAMER_RTL, "loc_seconds"),
        (FRAMER_RTL, "reg_tb_nominal"),
        (FRAMER_RTL, "tb_seconds_next"),
    ],
)
def test_constrained_registers_exist_in_the_rtl(rtl, reg):
    assert re.search(rf"\breg\b[^;]*\b{reg}\b", rtl), reg


def test_cdc_script_constrains_every_crossing_and_refuses_missing_cells():
    code = _code(CDC)
    for pattern in (
        "*tick_hop_gpio_sync*gpio_meta_reg*",
        "*vrt49_time_base_0*${stage}_meta_reg",
        "*axi_vrt49_framer_0*reg_tb_*_reg*",
        "*vrt49_time_base_0*cfg_*_meta_reg*",
        "*axi_vrt49_framer_0*tb_seconds_*_reg*",
        "*vrt49_time_base_0*/seconds_reg*",
        "*vrt49_time_base_0*/seconds_armed_reg*",
        "*axi_vrt49_framer_0*loc_*_reg*",
        "*vrt49_time_base_0*tuple_toggle_reg*",
        "*axi_vrt49_framer_0*tgl_meta_reg",
    ):
        assert pattern in code, pattern
    assert "foreach stage {pps ppssel secreq}" in code
    assert "pps_alt" not in code  # a constant input: its synchronizer is optimised away
    assert "set_clock_groups" not in code
    assert "tick_pps_select" not in code and "pmod0_d4" not in code
    assert code.count("error ") >= 5
    assert "set_false_path -to [hopcap_a9_d_pins $a9_gpio_meta]" in code
    assert "set_max_delay -datapath_only [expr {2.0 * $a9_divclk_period}]" in code
    assert "set_max_delay -datapath_only $a9_divclk_period -from $a9_sec_src" in code


def test_post_route_gate_checks_what_the_cdc_script_sets_and_the_iob_packing():
    code = _code(GATE)
    assert 'set a9_rpt_name "hopcap_adrv9361_post_route.rpt"' in code
    assert "{*tick_hop_gpio_sync*gpio_meta_reg*} {*tick_hop_gpio_sync*gpio_s1_reg*} 4" in code
    assert "foreach stage {pps ppssel secreq}" in code
    assert "{*tick_ctrl_in_iob_reg*}" in code and "OLOGIC" in code
    assert "HOPCAP_ADRV9361 ALL PASS" in code
    assert 'error "hopcap adrv9361 post-route gate failed' in code
    # same bounds as the constraint script
    assert "[expr {2.0 * $div_p}]" in code
    # the netlist checker reads the same report name
    import check_hop_netlist as chn

    assert chn.A9_GATE_REPORT == "hopcap_adrv9361_post_route.rpt"


# ---------------------------------------------------------------------------------------
# full assemble on a synthetic ADI tree (the ADI side is only a directory here)


def _git_init_commit(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )


@pytest.fixture
def a9_tree(tmp_path: Path) -> dict:
    adi = tmp_path / "hdl"
    (adi / aw.A9_TICK_PROJECT_REL).mkdir(parents=True)
    (adi / aw.A9_TICK_PROJECT_REL / "system_bd.tcl").write_text("# ADI\n")
    _git_init_commit(adi)
    cor = tmp_path / "corundum"
    cor.mkdir()
    _git_init_commit(cor)
    return {"adi": adi, "corundum": cor, "root": tmp_path}


def test_assemble_adrv9361_stages_patches_and_is_idempotent(a9_tree):
    adi = a9_tree["adi"]
    kw = dict(adi_hdl=adi, corundum=a9_tree["corundum"], check_pins=False, profile="adrv9361")
    m1 = aw.assemble(a9_tree["root"], **kw)
    proj = adi / aw.A9_PROJECT_REL
    assert m1["profile"] == "adrv9361" and m1["physical_port"] == "sfp"
    assert m1["contract_checks"] == []
    for ip in aw.A9_VRT49_IPS:
        assert (adi / "library" / ip).is_dir(), ip
    for gone in ("tick_vendor", "vrt49_ad9361_hop_ctrl"):
        assert not (adi / "library" / gone).exists()
    assert (adi / "library/tick_pps_gen/tick_pps_gen.v").exists()
    assert (adi / "library/tick_gpio_sync/tick_gpio_sync.v").exists()
    assert (adi / aw.TICK_LIB_REL / "axi_timed_command_scheduler.v").read_bytes() == (
        TICK / "hdl" / aw.TICK_LIB_REL / "axi_timed_command_scheduler.v"
    ).read_bytes()
    for name in (aw.A9_TICK_FRAGMENT, aw.A9_TICK_XDC):
        assert (adi / aw.A9_TICK_PROJECT_REL / name).exists()
    for name in aw.A9_PROJECT_FILES:
        assert (proj / name).read_bytes() == (ROOT / "hdl" / name).read_bytes()
    assert aw.A9_HOP_FRAGMENT_SOURCE in (proj / "system_bd.tcl").read_text()
    assert "tick_ctrl_in_iob" in (proj / "system_top.v").read_text()
    files = m1["files"]
    assert "projects/adrv9361z7035_vrt49_sfp/system_top.v" in files
    assert "projects/adrv9361z7035/ccfmc_lvds/tick_adrv9361z7035_ccfmc_lvds.tcl" in files
    assert "library/tick_pps_gen/tick_pps_gen_ip.tcl" in files
    # ADI's own files are not ours to hash
    assert "projects/adrv9361z7035/ccfmc_lvds/system_bd.tcl" not in files
    written = json.loads((a9_tree["root"] / "MANIFEST.json").read_text())
    assert written["files"] == files
    m2 = aw.assemble(a9_tree["root"], **kw)
    assert m2["files"] == m1["files"]


def test_assemble_adrv9361_refuses_qsfp_and_a_tree_without_the_board(a9_tree, tmp_path):
    with pytest.raises(aw.AssembleError, match="sfp only"):
        aw.assemble(
            a9_tree["root"],
            adi_hdl=a9_tree["adi"],
            corundum=a9_tree["corundum"],
            check_pins=False,
            profile="adrv9361",
            port="qsfp",
        )
    bare = tmp_path / "bare"
    bare.mkdir()
    _git_init_commit(bare)
    with pytest.raises(aw.AssembleError, match="ADRV9361"):
        aw.assemble(
            a9_tree["root"],
            adi_hdl=bare,
            corundum=a9_tree["corundum"],
            check_pins=False,
            profile="adrv9361",
        )
