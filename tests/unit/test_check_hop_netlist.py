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


# ---------------------------------------------------------------- timed-dma profile (P3b G1)


def tdma_instances() -> dict[str, tuple[str, dict[str, str]]]:
    """The timed-dma fragment's wiring as it appears in module system, by net."""
    t = "rx_adrv9009_som_tpl_core"
    inst = {
        t: (
            "rx_adrv9009_som_tpl_core_imp_134OMCX",
            {
                **{f"adc_data_{i}": f"{t}_adc_data_{i}" for i in range(8)},
                "adc_valid_0": f"{t}_adc_valid_0",
                "adc_rst": f"{t}_adc_rst",
                "link_clk": "core_clk_b",
            },
        ),
        "util_som_rx_cpack": (
            "system_util_som_rx_cpack_0",
            {
                "clk": "core_clk_b",
                **{f"fifo_wr_data_{i}": f"{t}_adc_data_{i}" for i in range(8)},
                "fifo_wr_en": "tick_rx_dma_gate_rx_valid_out",
                "packed_fifo_wr_data": "util_som_rx_cpack_packed_fifo_wr_data",
                "packed_fifo_wr_en": "util_som_rx_cpack_packed_fifo_wr_en",
                "packed_fifo_wr_overflow": "tick_rx_tag_adapter_s_fifo_wr_overflow",
                "packed_sync": "util_som_rx_cpack_packed_sync",
                "reset": "tick_rx_pack_reset_or_Res",
            },
        ),
        "tick_rx_dma_gate": (
            "system_tick_rx_dma_gate_0",
            {
                "dev_clk": "core_clk_b",
                "dev_rst": f"{t}_adc_rst",
                "gate_level": "tick_gate_rx_level_Dout",
                "gate_mode": "tick_gate_rx_mode_Dout",
                "mode_dev": "tick_rx_dma_gate_mode_dev",
                "pack_reset": "tick_rx_dma_gate_pack_reset",
                "pass_dev": "tick_rx_dma_gate_pass_dev",
                "rx_valid_in": f"{t}_adc_valid_0",
                "rx_valid_out": "tick_rx_dma_gate_rx_valid_out",
                "tick_clk": "core_clk_a",
                "tx_data_in": "tick_tdma_zero_256_dout",
                "tx_rd_en_in": "tick_tdma_zero_1_dout",
            },
        ),
        "tick_rx_pack_reset_or": (
            "system_tick_rx_pack_reset_or_0",
            {
                "Op1": f"{t}_adc_rst",
                "Op2": "tick_rx_dma_gate_pack_reset",
                "Res": "tick_rx_pack_reset_or_Res",
            },
        ),
        "vrt49_adc_reset_inv": (
            "system_vrt49_adc_reset_inv_0",
            {"Op1": f"{t}_adc_rst", "Res": "inv_res"},
        ),
        "vrt49_rx_tap": (
            "system_vrt49_rx_tap_0",
            {"fifo_wr_en": f"{t}_adc_valid_0", "fifo_wr_data": "vrt49_rx_iq0_concat_dout"},
        ),
        "vrt49_rx_iq0_concat": (
            "system_vrt49_rx_iq0_concat_0",
            {
                "In0": f"{t}_adc_data_0",
                "In1": f"{t}_adc_data_1",
                "dout": "vrt49_rx_iq0_concat_dout",
            },
        ),
        # same instances as PPS_BLOCK, so the PPS checks still see their nets
        "vrt49_time_base_0": (
            "system_vrt49_time_base_0_0",
            {
                "sample_valid": f"{t}_adc_valid_0",
                "pps_alt_in": "vrt49_external_pps_slice_Dout",
                "pps_in": "corundum_hierarchy_ptp_pps_str",
            },
        ),
        "tick_rx_tag_adapter": (
            "system_tick_rx_tag_adapter_0",
            {
                "clk": "core_clk_b",
                "rst": f"{t}_adc_rst",
                "s_fifo_wr_en": "util_som_rx_cpack_packed_fifo_wr_en",
                "s_fifo_wr_data": "util_som_rx_cpack_packed_fifo_wr_data",
                "s_fifo_wr_sync": "util_som_rx_cpack_packed_sync",
                "s_fifo_wr_overflow": "tick_rx_tag_adapter_s_fifo_wr_overflow",
                "m_fifo_wr_en": "tick_rx_tag_adapter_m_fifo_wr_en",
                "m_fifo_wr_data": "tick_rx_tag_adapter_m_fifo_wr_data",
                "m_fifo_wr_sync": "tick_rx_tag_adapter_m_fifo_wr_sync",
                "m_fifo_wr_overflow": "axi_adrv9009_som_rx_dma_fifo_wr_overflow",
                "pass_dev": "tick_rx_dma_gate_pass_dev",
                "mode_dev": "tick_rx_dma_gate_mode_dev",
                "tag_clk": "core_clk_a",
                **{
                    f"tag_{f}": f"axi_timed_command_scheduler_0_capture_tag_{f}"
                    for f in ("ticks", "seq", "target", "command", "gate", "count", "late", "valid")
                },
                "tag_oneshot": "axi_timed_command_scheduler_0_stream_tag_oneshot",
                "tag_enable": "tick_tag_en_slice_Dout",
                "tag_tick_hz": "tick_tag_tick_hz_dout",
                "dbg_status": "tick_rx_tag_adapter_dbg_status",
            },
        ),
        "axi_adrv9009_som_rx_dma": (
            "system_axi_adrv9009_som_rx_dma_0",
            {
                "fifo_wr_clk": "core_clk_b",
                "fifo_wr_din": "tick_rx_tag_adapter_m_fifo_wr_data",
                "fifo_wr_en": "tick_rx_tag_adapter_m_fifo_wr_en",
                "fifo_wr_overflow": "axi_adrv9009_som_rx_dma_fifo_wr_overflow",
                "sync": "tick_rx_tag_adapter_m_fifo_wr_sync",
            },
        ),
        "axi_timed_command_scheduler_0": (
            "system_axi_timed_command_scheduler_0_0",
            {
                "s_axi_aclk": "core_clk_a",
                "pps_in": "tick_pps_select_0_pps_out",
                "stream_gate_en": "axi_timed_command_scheduler_0_stream_gate_en",
                "stream_tag_en": "axi_timed_command_scheduler_0_stream_tag_en",
                "stream_tag_oneshot": "axi_timed_command_scheduler_0_stream_tag_oneshot",
                "stream_tag_probe": "tick_rx_tag_adapter_dbg_status",
                **{
                    f"capture_tag_{f}": f"axi_timed_command_scheduler_0_capture_tag_{f}"
                    for f in ("ticks", "seq", "target", "command", "gate", "count", "late", "valid")
                },
            },
        ),
        "tick_tag_en_slice": (
            "system_tick_tag_en_slice_0",
            {
                "Din": "axi_timed_command_scheduler_0_stream_tag_en",
                "Dout": "tick_tag_en_slice_Dout",
            },
        ),
        "tick_tx_dma_gate": (
            "system_tick_tx_dma_gate_0",
            {
                "dev_clk": "core_clk_a",
                "dev_rst": "tx_adrv9009_som_tpl_core_dac_rst",
                "gate_level": "tick_gate_tx_level_Dout",
                "gate_mode": "tick_gate_tx_mode_Dout",
                "rx_valid_in": "tick_tdma_zero_1_dout",
                "tick_clk": "core_clk_a",
                "tx_data_in": "tick_tx_gate_data_concat_dout",
                "tx_data_out": "tick_tx_dma_gate_tx_data_out",
                "tx_rd_en_in": "tx_adrv9009_som_tpl_core_dac_valid_0",
                "tx_rd_en_out": "tick_tx_dma_gate_tx_rd_en_out",
            },
        ),
        "util_som_tx_upack": (
            "system_util_som_tx_upack_0",
            {
                "clk": "core_clk_a",
                "fifo_rd_en": "tick_tx_dma_gate_tx_rd_en_out",
                **{f"fifo_rd_data_{i}": f"util_som_tx_upack_fifo_rd_data_{i}" for i in range(8)},
            },
        ),
        "tx_adrv9009_som_tpl_core": (
            "tx_adrv9009_som_tpl_core_imp_16NPXD",
            {
                "dac_rst": "tx_adrv9009_som_tpl_core_dac_rst",
                "dac_valid_0": "tx_adrv9009_som_tpl_core_dac_valid_0",
                **{f"dac_data_{i}": f"tick_tx_gate_data_slice_{i}_Dout" for i in range(8)},
            },
        ),
        "tick_tx_gate_data_concat": (
            "system_tick_tx_gate_data_concat_0",
            {
                **{f"In{i}": f"util_som_tx_upack_fifo_rd_data_{i}" for i in range(8)},
                "dout": "tick_tx_gate_data_concat_dout",
            },
        ),
        "axi_adrv9009_som_tx_dma": (
            "system_axi_adrv9009_som_tx_dma_0",
            {
                "m_axis_data": "axi_adrv9009_som_tx_dma_m_axis_data",
                "m_axis_valid": "axi_adrv9009_som_tx_dma_m_axis_valid",
                "m_axis_ready": "axi_tx_fifo_dma_ready",
                "m_axis_last": "axi_adrv9009_som_tx_dma_m_axis_last",
                "m_axis_xfer_req": "axi_adrv9009_som_tx_dma_m_axis_xfer_req",
            },
        ),
        "axi_tx_fifo": (
            "system_axi_tx_fifo_0",
            {
                "dma_data": "axi_adrv9009_som_tx_dma_m_axis_data",
                "dma_valid": "axi_adrv9009_som_tx_dma_m_axis_valid",
                "dma_ready": "axi_tx_fifo_dma_ready",
                "dma_xfer_last": "axi_adrv9009_som_tx_dma_m_axis_last",
                "dma_xfer_req": "axi_adrv9009_som_tx_dma_m_axis_xfer_req",
            },
        ),
    }
    inst["tick_fhm_gpio_slice"] = (
        "system_tick_fhm_gpio_slice_0",
        {"Din": "axi_timed_command_scheduler_0_gpio_out", "Dout": "tick_fhm_gpio_slice_Dout"},
    )
    inst["tick_hop_gpio_sync"] = (
        "system_tick_hop_gpio_sync_0",
        {
            "clk": "sys_cpu_clk",
            "gpio_async": "tick_fhm_gpio_slice_Dout",
            "gpio_sync": "tick_hop_gpio_sync_gpio_sync",
        },
    )
    inst["vrt49_fhm_hop_ctrl_0"] = (
        "system_vrt49_fhm_hop_ctrl_0_0",
        {"clk": "sys_cpu_clk", "scheduler_gpio": "tick_hop_gpio_sync_gpio_sync"},
    )
    for name in (
        "tick_gate_rx_level",
        "tick_gate_tx_level",
        "tick_gate_rx_mode",
        "tick_gate_tx_mode",
    ):
        inst[name] = (
            f"system_{name}_0",
            {"Din": "axi_timed_command_scheduler_0_stream_gate_en", "Dout": f"{name}_Dout"},
        )
    for i in range(8):
        inst[f"tick_tx_gate_data_slice_{i}"] = (
            f"system_tick_tx_gate_data_slice_{i}_0",
            {"Din": "tick_tx_dma_gate_tx_data_out", "Dout": f"tick_tx_gate_data_slice_{i}_Dout"},
        )
    return inst


def render(inst) -> str:
    body = []
    for name, (kind, conns) in inst.items():
        ports = ",\n        ".join(f".{p}({n})" for p, n in sorted(conns.items()))
        body.append(f"  {kind} {name}\n       ({ports});")
    return "module system\n   (gpio_i);\n" + "\n".join(body) + "\nendmodule\n"


def test_timed_dma_wiring_passes():
    assert chn.check_timed_dma(render(tdma_instances())) == []


@pytest.mark.parametrize(
    ("inst", "port", "net", "expect"),
    [
        # the spec's original insertion: cpack fed straight from the TPL (gate bypassed)
        (
            "util_som_rx_cpack",
            "fifo_wr_en",
            "rx_adrv9009_som_tpl_core_adc_valid_0",
            "bypasses the RX gate",
        ),
        # G2(d): replacing the adc_valid_0 NET gates the VRT tap and its time base
        ("vrt49_rx_tap", "fifo_wr_en", "tick_rx_dma_gate_rx_valid_out", "vrt49 rx tap"),
        ("vrt49_time_base_0", "sample_valid", "tick_rx_dma_gate_rx_valid_out", "time base"),
        # adc_rst: cpack still on the raw reset, or vrt49's reset moved
        ("util_som_rx_cpack", "reset", "rx_adrv9009_som_tpl_core_adc_rst", "cpack reset"),
        ("vrt49_adc_reset_inv", "Op1", "tick_rx_pack_reset_or_Res", "adc_rst net"),
        # tagger bypassed / not fed by the scheduler
        (
            "axi_adrv9009_som_rx_dma",
            "fifo_wr_din",
            "util_som_rx_cpack_packed_fifo_wr_data",
            "tagger",
        ),
        ("axi_adrv9009_som_rx_dma", "sync", "util_som_rx_cpack_packed_sync", "adapter -> RX DMA"),
        ("tick_rx_tag_adapter", "tag_ticks", "tick_tdma_zero_256_dout", "capture tag"),
        ("tick_rx_tag_adapter", "tag_enable", "1'b1", "tag_enable"),
        # gate bits from somewhere else
        ("tick_rx_dma_gate", "gate_mode", "tick_gate_tx_mode_Dout", "mode bit"),
        ("tick_tx_dma_gate", "dev_clk", "core_clk_b", "device clock"),
        # TX lane or read enable bypassing the gate
        ("tx_adrv9009_som_tpl_core", "dac_data_3", "util_som_tx_upack_fifo_rd_data_3", "TX lane"),
        ("util_som_tx_upack", "fifo_rd_en", "tx_adrv9009_som_tpl_core_dac_valid_0", "TX gate"),
        # Tick left on the PS clock (TICK_TIMEBASE_CLK not applied)
        ("axi_timed_command_scheduler_0", "s_axi_aclk", "sys_cpu_clk", "core_clk_a"),
        # hop controller fed straight from Tick's slice across the clock domains
        ("vrt49_fhm_hop_ctrl_0", "scheduler_gpio", "tick_fhm_gpio_slice_Dout", "sync"),
        ("tick_hop_gpio_sync", "clk", "core_clk_a", "hop GPIO sync clock"),
        # vrt49's TX bridge left in front of axi_tx_fifo
        ("axi_tx_fifo", "dma_data", "vrt49_dac_tx_0_m_axis_tdata", "stock TX DMA"),
    ],
)
def test_timed_dma_wiring_faults_are_caught(inst, port, net, expect):
    insts = tdma_instances()
    insts[inst][1][port] = net
    errs = chn.check_timed_dma(render(insts))
    assert any(expect in e for e in errs), errs


def test_timed_dma_requires_both_gates_and_no_vrt49_dac_tx():
    insts = tdma_instances()
    del insts["tick_tx_dma_gate"]
    assert any("tick_tx_dma_gate missing" in e for e in chn.check_timed_dma(render(insts)))
    insts = tdma_instances()
    insts["vrt49_dac_tx_0"] = ("system_vrt49_dac_tx_0_0", {"clk": "core_clk_a"})
    assert any("VRT_DAC_TX_ENABLE" in e for e in chn.check_timed_dma(render(insts)))


def _wrappers(proj: Path, override: dict | None = None) -> None:
    for cell, (module, params) in chn.TDMA_WRAPPERS.items():
        params = {**params, **(override or {}).get(cell, {})}
        d = proj / f"x.gen/sources_1/bd/system/ip/system_{cell}_0/synth"
        d.mkdir(parents=True, exist_ok=True)
        kind = "xlslice_v1_0_5_xlslice" if module == "xlslice" else module
        plist = ",\n    ".join(f".{k}({v})" for k, v in params.items())
        (d / f"system_{cell}_0.v").write_text(
            f"module system_{cell}_0 (a);\n  {kind} #(\n    {plist}\n  ) inst (\n    .a(a)\n  );\nendmodule\n"
        )


TDMA_TOP = (
    GOOD_TOP.replace("ad_iobuf #(.DATA_WIDTH(60)) x();\n", "")
    + "  ad_iobuf #(\n    .DATA_WIDTH(34)\n  ) i_iobuf (\n    .dio_t ({gpio_t[92:59]}),\n"
    + "  ad_iobuf #(\n    .DATA_WIDTH(25)\n  ) i_iobuf_tick_fhm_b (\n    .dio_t ({gpio_t[57:33]}),\n"
    + "  assign adrv9009_gpio_00_b = tick_fhm_gpio[1];\n  assign gpio_i[58] = tick_fhm_gpio[1];\n"
)


def test_timed_dma_profile_end_to_end(tmp_path):
    net = GOOD_NET.replace("endmodule\n", "") + render(tdma_instances()).split("(gpio_i);\n", 1)[1]
    proj, top = _tree(tmp_path, net, TDMA_TOP)
    _wrappers(proj)
    assert chn.check(proj, top, "timed-dma") == []
    # the default profile keeps asserting Tick's unsplit IOBUF
    assert any("DATA_WIDTH(60)" in e for e in chn.check(proj, top))


def test_timed_dma_profile_checks_wrappers_and_the_chip_b_top(tmp_path):
    net = GOOD_NET.replace("endmodule\n", "") + render(tdma_instances()).split("(gpio_i);\n", 1)[1]
    proj, top = _tree(tmp_path, net, GOOD_TOP)
    _wrappers(
        proj, {"tick_rx_dma_gate": {"OPEN_DELAY": "6"}, "tick_gate_tx_mode": {"DIN_FROM": "2"}}
    )
    errs = chn.check(proj, top, "timed-dma")
    assert any("OPEN_DELAY = 6" in e for e in errs)
    assert any("tick_gate_tx_mode: DIN_FROM" in e for e in errs)
    assert any("assign adrv9009_gpio_00_b" in e for e in errs)
    (
        proj
        / "x.gen/sources_1/bd/system/ip/system_tick_rx_tag_adapter_0/synth/system_tick_rx_tag_adapter_0.v"
    ).unlink()
    assert any(
        "no generated wrapper for BD cell tick_rx_tag_adapter" in e
        for e in chn.check(proj, top, "timed-dma")
    )


def test_cli_reports_the_profile(tmp_path):
    import subprocess

    net = GOOD_NET.replace("endmodule\n", "") + render(tdma_instances()).split("(gpio_i);\n", 1)[1]
    proj = tmp_path / "projects/adrv9009zu11eg_vrt49_corundum"
    (proj / "synth").mkdir(parents=True)
    (proj / "synth/system.v").write_text(net)
    _wrappers(proj)
    top = tmp_path / chn.PORT_TOP["sfp"]
    top.parent.mkdir(parents=True, exist_ok=True)
    top.write_text(TDMA_TOP)
    r = subprocess.run(
        [sys.executable, chn.__file__, str(tmp_path), "--port", "sfp", "--profile", "timed-dma"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert (
        "check_hop_netlist: profile timed-dma: OK" in r.stdout
        and "check_hop_netlist: OK" in r.stdout
    )
