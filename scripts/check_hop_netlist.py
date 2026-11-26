#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Post-synthesis assertions for the hopcap composite build.

Reads the Vivado project's synthesized netlist (``synth/system.v`` under the project dir,
as vrt49's check-sfp-rx-netlist.py does) and the staged top-level, and fails closed unless:
  * vrt49_fhm_hop_ctrl is instantiated and its fabric nets reach axi_vrt49_framer;
  * axi_timed_command_scheduler is instantiated;
  * the top-level drives adrv9009_gpio_00_a from tick_fhm_gpio[0];
  * Tick's pps_in is driven by tick_pps_select's pps_out (not Tick's constant tie-off), the
    selector's int_pps_async is Corundum's ptp_pps_str on the same net as vrt49_time_base_0's
    pps_in, and its ext_pps_async is vrt49's external PPS (gpio_i[16]) slice.

The PPS checks read port connections (``.pin(net)``) of the instances in the BD's top
``module system``, so they follow nets rather than matching names alone.

``--profile timed-dma`` (libdatax P3b candidate A, hdl/system_bd_timed_dma.tcl) additionally
requires, again by net:
  * both tick_dma_gate instances (module-reference wrappers checked for module and parameters),
    clocked by the cpack/upack clocks and fed by Tick's stream_gate_en bits 0..3;
  * util_som_rx_cpack.fifo_wr_en driven by the RX gate, whose input is the TPL adc_valid_0;
  * vrt49's RX tap still on the TPL nets (vrt49_rx_tap.fifo_wr_en and vrt49_time_base_0
    .sample_valid on adc_valid_0, the I/Q concat on adc_data_0/1 and into the tap): the
    invariants of vrt49's check-sfp-rx-netlist.py that this profile keeps (that script also
    asserts cpack -> DMA directly, which the profile replaces, so build_bitstream.sh skips it);
  * the adc_rst net untouched (vrt49_adc_reset_inv and the RX gate on it), with only the cpack
    reset pin moved to adc_rst | pack_reset;
  * tick_rx_tag_adapter between cpack's packed_fifo_wr/packed_sync and the RX DMA, fed by the
    scheduler's capture_tag_* outputs and stream_tag_en[0];
  * the TX gate between util_som_tx_upack and the TX TPL (read enable and all data lanes);
  * the stock axi_adrv9009_som_tx_dma -> axi_tx_fifo nets, and no vrt49_dac_tx_0;
  * the chip-B FHM assigns and the split Talise GPIO IOBUF in the top-level.

``--profile adrv9361`` (ADRV9361-Z7035, hdl/system_bd_adrv9361_hop.tcl on vrt49's
adrv9361z7035_vrt49_sfp) checks a different project and requires, by net and by the generated
wrapper parameters:
  * no vrt49_ad9361_hop_ctrl (HOP1) and no vrt49 tick_scheduler_0 anywhere in the netlist;
  * axi_timed_command_scheduler_0 a *module reference* (wrapper VLNV module_ref) at
    TIME_TICK_HZ 50000000, its gpio_out[2:0] on the tick_ctrl_in port;
  * vrt49_fhm_hop_ctrl_0 (HOP2) fed {gpio_out[2:0], gpio_out[3]} through tick_hop_gpio_sync
    on its own clock (not Tick's), and the seven fabric nets to axi_vrt49_framer_0;
  * tick_pps_gen_0 (TICK_HZ 50000000) on Tick's clock driving Tick's pps_in and
    vrt49_time_base_0's pps_in; the time base on the tap's clock and sample_valid, its
    tuple/config buses on the framer, pps_alt_in tied off;
  * vrt49_link_throttle FORWARD_EVERY_N 1;
  * the top-level's CTRL_IN IOB register block and tick_clk/tick_ctrl_in wrapper ports;
  * the post-route gate report (hdl/check_adrv9361_hop.tcl): CDC exceptions applied and the
    three CTRL_IN registers placed in OLOGIC sites, ending in "HOPCAP_ADRV9361 ALL PASS".
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Plain substrings: Vivado names BD instances like system_vrt49_fhm_hop_ctrl_0_0, where a
# regex word boundary would never match.
REQUIRED_NETLIST = (
    "vrt49_fhm_hop_ctrl",
    "axi_timed_command_scheduler",
    "axi_vrt49_framer",
    "fabric_rf_frequency_q44_20",
    "fabric_context_accepted",
    "tick_pps_select",
)
# The top-level each port builds; it must match assemble_workspace.PORT_TOP (a unit test checks).
# Kept here too because build_bitstream_remote.sh ships only this script to the build host.
PORT_TOP = {
    "sfp": "projects/adrv9009zu11eg_vrt49_corundum/system_top_sfp.v",
    "qsfp": "projects/adrv9009zu11eg/adrv2crr_fmc/system_top_corundum.v",
}
REQUIRED_TOP = (
    "assign adrv9009_gpio_00_a = tick_fhm_gpio[0];",
    ".tick_fhm_gpio (tick_fhm_gpio),",
    ".DATA_WIDTH(60)",
)
PROFILES = ("default", "timed-dma", "adrv9361")
# --- adrv9361 ---------------------------------------------------------------------------
A9_PROJECT = "projects/adrv9361z7035_vrt49_sfp"
A9_TOP = f"{A9_PROJECT}/system_top.v"
A9_GATE_REPORT = "hopcap_adrv9361_post_route.rpt"
A9_TICK_HZ = "50000000"
REQUIRED_NETLIST_A9 = (
    "vrt49_fhm_hop_ctrl",
    "axi_timed_command_scheduler",
    "axi_vrt49_framer",
    "vrt49_time_base",
    "tick_pps_gen",
    "tap_accept",
    "tick_hop_gpio_sync",  # module reference: its type is checked through the wrapper VLNV
)
FORBIDDEN_NETLIST_A9 = ("vrt49_ad9361_hop_ctrl", "tick_scheduler_0", "fastlock_select")
REQUIRED_TOP_A9 = (
    '(* IOB = "TRUE" *) reg [2:0] tick_ctrl_in_iob',
    "tick_ctrl_in_iob <= tick_ctrl_in;",
    "always @(posedge tick_clk) begin",
    ".dio_i (tick_ctrl_in_iob),",
    ".dio_o (gpio_i[43:41]),",
    ".dio_p (gpio_ctl[3:1]));",
    "gpio_ctl[0],        // 40:40 / CTRL_IN0 (PS-owned; hopcap)",
    ".tick_clk (tick_clk),",
    ".tick_ctrl_in (tick_ctrl_in),",
)
FORBIDDEN_TOP_A9 = (
    "ad9361_fastlock_select",
    "assign gpio_ctl[2:0]",
    ".dio_p (gpio_ctl[2:0]))",
    "gpio_ctl[3],        // 43:43",
)
# cell -> (wrapped module, {parameter: value}); module references must be module_ref VLNVs.
A9_WRAPPERS = {
    "axi_timed_command_scheduler_0": (
        "axi_timed_command_scheduler",
        {"TIME_TICK_HZ": A9_TICK_HZ, "GPIO_WIDTH": "4"},
    ),
    "vrt49_link_throttle": ("vrt49_frame_throttle", {"FORWARD_EVERY_N": "1"}),
    "tick_pps_gen_0": ("tick_pps_gen", {"TICK_HZ": A9_TICK_HZ, "PULSE_TICKS": "5000000"}),
    "tick_hop_event_slice": ("xlslice", {"DIN_WIDTH": "4", "DIN_FROM": "3", "DIN_TO": "3"}),
    "tick_hop_slot_slice": ("xlslice", {"DIN_WIDTH": "4", "DIN_FROM": "2", "DIN_TO": "0"}),
    "tick_ctrl_in_slice": ("xlslice", {"DIN_WIDTH": "4", "DIN_FROM": "2", "DIN_TO": "0"}),
}
A9_MODULE_REFS = {
    "axi_timed_command_scheduler_0": "axi_timed_command_scheduler",
    "tick_hop_gpio_sync": "tick_gpio_sync",
}
# timed-dma: Tick's chip-A edit plus the chip-B split (assemble_workspace.CHIP_B_EDITS).
REQUIRED_TOP_TDMA = (
    "assign adrv9009_gpio_00_a = tick_fhm_gpio[0];",
    ".tick_fhm_gpio (tick_fhm_gpio),",
    "assign adrv9009_gpio_00_b = tick_fhm_gpio[1];",
    "assign gpio_i[58] = tick_fhm_gpio[1];",
    ".DATA_WIDTH(34)",
    ".dio_t ({gpio_t[92:59]}),",
    ".DATA_WIDTH(25)",
    ".dio_t ({gpio_t[57:33]}),",
)
FORBIDDEN_TOP_TDMA = ("gpio_t[92:33]", "adrv9009_gpio_00_b}));", "adrv9009_gpio_00_b,       // 58")
# Module-reference / IP parameters the timed-dma fragment sets, read back from each cell's
# generated wrapper (<project>.gen/.../bd/system/ip/system_<cell>_0/synth/system_<cell>_0.v).
TDMA_WRAPPERS = {
    "axi_timed_command_scheduler_0": (
        "axi_timed_command_scheduler",
        # TICK_TIMEBASE_CLK=core_clk_a: one tick per two RX samples (P3b option 1, sdk#40)
        {"STREAM_GATE_COUNT": "8", "HAS_STREAM_TIMESTAMP": "1", "TIME_TICK_HZ": "122880000"},
    ),
    "tick_rx_dma_gate": (
        "tick_dma_gate",
        {"DIRECTION": "0", "SRC_REG": "1", "OPEN_DELAY": "12"},
    ),
    "tick_tx_dma_gate": (
        "tick_dma_gate",
        {
            "DIRECTION": "1",
            "TX_DATA_WIDTH": "256",
            "SRC_REG": "1",
            "TX_PAUSE_READS": "0",
            "TX_START_MUTE": "3",
        },
    ),
    "tick_rx_tag_adapter": (
        "tick_fifo_tag_adapter",
        {
            "DATA_WIDTH": "128",
            "TAG_GATE_ID": "0",
            "ENABLE_TAGGER": "1",
            "DATA_DELAY": "0",
            "REARM_ON_PASS": "1",
        },
    ),
    # stream_gate_en bit -> gate input (P3b decision 2)
    "tick_gate_rx_level": ("xlslice", {"DIN_WIDTH": "8", "DIN_FROM": "0", "DIN_TO": "0"}),
    "tick_gate_tx_level": ("xlslice", {"DIN_WIDTH": "8", "DIN_FROM": "1", "DIN_TO": "1"}),
    "tick_gate_rx_mode": ("xlslice", {"DIN_WIDTH": "8", "DIN_FROM": "2", "DIN_TO": "2"}),
    "tick_gate_tx_mode": ("xlslice", {"DIN_WIDTH": "8", "DIN_FROM": "3", "DIN_TO": "3"}),
    "tick_tag_en_slice": ("xlslice", {"DIN_WIDTH": "8", "DIN_FROM": "0", "DIN_TO": "0"}),
}


_INSTANCE = re.compile(r"^[ \t]*(\w+)[ \t]+(\w+)\s*\(\s*(\..*?)\);", re.M | re.S)
_CONN = re.compile(r"\.(\w+)\(([^()]*)\)")
_LITERAL = re.compile(r"\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_]+|\d+")
# Corundum's free-running PTP PPS as it leaves corundum_core / the corundum hierarchy (Vivado
# may suffix a hierarchy pin).  ptp_sync_pps_str is a different, core-clock signal.
_CORUNDUM_PPS_PORT = re.compile(r"ptp_pps_str(_?\d+)?")


def top_instances(netlist: str) -> dict[str, tuple[str, dict[str, str]]]:
    """Instances of the BD's top ``module system``: name -> (module type, {port: net})."""
    m = re.search(r"^module system\b.*?^endmodule", netlist, re.M | re.S)
    body = m.group(0) if m else ""
    out: dict[str, tuple[str, dict[str, str]]] = {}
    for im in _INSTANCE.finditer(body):
        kind, name, conns = im.groups()
        if kind in {"module", "input", "output", "inout", "wire", "assign"}:
            continue
        out[name] = (kind, {p: re.sub(r"\s+", "", n) for p, n in _CONN.findall(conns)})
    return out


def _constant_drivers(insts) -> set[str]:
    nets = set()
    for name, (kind, conns) in insts.items():
        if re.search(r"tieoff|const|GND|VCC|xlconstant", f"{name} {kind}") and set(conns) == {
            "dout"
        }:
            nets.add(conns["dout"])
    return nets


def check_pps(netlist: str) -> list[str]:
    insts = top_instances(netlist)
    errors: list[str] = []

    def one(substr: str):
        hits = [(n, c) for n, (k, c) in insts.items() if substr in k]
        if len(hits) != 1:
            errors.append(f"expected one {substr} instance in module system, found {len(hits)}")
            return None
        return hits[0]

    tick, sel = one("axi_timed_command_scheduler"), one("tick_pps_select")
    if tick is None or sel is None:
        return errors
    (tick_name, tick_c), (sel_name, sel_c) = tick, sel
    pps_in, pps_out = tick_c.get("pps_in"), sel_c.get("pps_out")
    consts = _constant_drivers(insts)
    if not pps_in:
        errors.append(f"{tick_name}.pps_in is unconnected")
    elif _LITERAL.fullmatch(pps_in) or pps_in in consts:
        errors.append(f"{tick_name}.pps_in is driven by a constant ({pps_in})")
    elif pps_in != pps_out:
        errors.append(f"{tick_name}.pps_in ({pps_in}) is not {sel_name}.pps_out ({pps_out})")

    int_net, ext_net = sel_c.get("int_pps_async"), sel_c.get("ext_pps_async")
    corundum = [
        (n, p)
        for n, (k, c) in insts.items()
        if "corundum" in f"{n} {k}".lower()
        for p, net in c.items()
        if net == int_net and _CORUNDUM_PPS_PORT.fullmatch(p)
    ]
    # vrt49's time base is a packaged IP, so always an instance: Tick must share its PPS nets.
    tb = insts.get("vrt49_time_base_0", ("", {}))[1]
    if not int_net:
        errors.append(f"{sel_name}.int_pps_async is unconnected")
    elif not corundum:
        errors.append(
            f"{sel_name}.int_pps_async ({int_net}) does not trace to Corundum's ptp_pps_str"
        )
    elif int_net != tb.get("pps_in"):
        errors.append(
            f"{sel_name}.int_pps_async ({int_net}) is not vrt49_time_base_0.pps_in "
            f"({tb.get('pps_in')}): Tick and VRT would not share a second"
        )
    # The external PPS is vrt49's gpio_i[16] slice, seen either at the slice instance or at the
    # time base's pps_alt_in (in case Vivado ever inlines xlslice the way it inlines xlconcat).
    slice_out = insts.get("vrt49_external_pps_slice", ("", {}))[1].get("Dout")
    if not ext_net or ext_net not in {slice_out, tb.get("pps_alt_in")} - {None}:
        errors.append(
            f"{sel_name}.ext_pps_async ({ext_net}) is neither vrt49_external_pps_slice.Dout "
            f"({slice_out}) nor vrt49_time_base_0.pps_alt_in ({tb.get('pps_alt_in')})"
        )
    return errors


def check_timed_dma(netlist: str) -> list[str]:
    """Net-level assertions for the timed-dma fragment (see the module docstring)."""
    insts = top_instances(netlist)
    errors: list[str] = []

    def pin(inst: str, port: str) -> str | None:
        if inst not in insts:
            errors.append(f"instance {inst} missing from module system")
            return None
        net = insts[inst][1].get(port)
        if not net or _LITERAL.fullmatch(net):
            errors.append(f"{inst}.{port} is unconnected or tied off ({net})")
            return None
        return net

    def same(a: tuple[str, str], b: tuple[str, str], why: str) -> None:
        na, nb = pin(*a), pin(*b)
        if na is not None and nb is not None and na != nb:
            errors.append(f"{why}: {a[0]}.{a[1]} ({na}) != {b[0]}.{b[1]} ({nb})")

    def differ(a: tuple[str, str], b: tuple[str, str], why: str) -> None:
        na, nb = pin(*a), pin(*b)
        if na is not None and na == nb:
            errors.append(f"{why}: {a[0]}.{a[1]} and {b[0]}.{b[1]} share {na}")

    tpl, cpack, rxg = "rx_adrv9009_som_tpl_core", "util_som_rx_cpack", "tick_rx_dma_gate"
    adp, dma, sched = (
        "tick_rx_tag_adapter",
        "axi_adrv9009_som_rx_dma",
        "axi_timed_command_scheduler_0",
    )
    ttpl, upack, txg = "tx_adrv9009_som_tpl_core", "util_som_tx_upack", "tick_tx_dma_gate"

    # both gates, clocked like the packers they sit on, level/mode from stream_gate_en
    for gate, packer, lvl, mode in (
        (rxg, cpack, "tick_gate_rx_level", "tick_gate_rx_mode"),
        (txg, upack, "tick_gate_tx_level", "tick_gate_tx_mode"),
    ):
        same((gate, "dev_clk"), (packer, "clk"), f"{gate} device clock")
        same((gate, "tick_clk"), (sched, "s_axi_aclk"), f"{gate} tick clock")
        same((gate, "gate_level"), (lvl, "Dout"), f"{gate} level bit")
        same((gate, "gate_mode"), (mode, "Dout"), f"{gate} mode bit")
        same((lvl, "Din"), (sched, "stream_gate_en"), f"{lvl} source")
        same((mode, "Din"), (sched, "stream_gate_en"), f"{mode} source")

    # RX: the cpack fifo_wr_en PIN is gated; the TPL adc_valid_0 NET is not
    same((rxg, "rx_valid_in"), (tpl, "adc_valid_0"), "RX gate input")
    same((cpack, "fifo_wr_en"), (rxg, "rx_valid_out"), "cpack fifo_wr_en driven by the RX gate")
    differ((cpack, "fifo_wr_en"), (tpl, "adc_valid_0"), "cpack fifo_wr_en bypasses the RX gate")
    for i in range(8):
        same((cpack, f"fifo_wr_data_{i}"), (tpl, f"adc_data_{i}"), "cpack data (not gated)")
    # vrt49's RX tap and time base stay on the TPL nets
    same(("vrt49_rx_tap", "fifo_wr_en"), (tpl, "adc_valid_0"), "vrt49 rx tap valid")
    same(("vrt49_time_base_0", "sample_valid"), (tpl, "adc_valid_0"), "vrt49 time base valid")
    same(("vrt49_rx_iq0_concat", "In0"), (tpl, "adc_data_0"), "vrt49 tap I/Q 0")
    same(("vrt49_rx_iq0_concat", "In1"), (tpl, "adc_data_1"), "vrt49 tap I/Q 1")
    same(("vrt49_rx_iq0_concat", "dout"), ("vrt49_rx_tap", "fifo_wr_data"), "vrt49 tap data")
    # adc_rst net untouched; only the cpack reset pin moves to adc_rst | pack_reset
    same(("vrt49_adc_reset_inv", "Op1"), (tpl, "adc_rst"), "adc_rst net (vrt49 reset)")
    same((rxg, "dev_rst"), (tpl, "adc_rst"), "RX gate reset")
    same(("tick_rx_pack_reset_or", "Op1"), (tpl, "adc_rst"), "cpack reset OR adc_rst input")
    same(("tick_rx_pack_reset_or", "Op2"), (rxg, "pack_reset"), "cpack reset OR pack_reset")
    same((cpack, "reset"), ("tick_rx_pack_reset_or", "Res"), "cpack reset pin")
    differ((cpack, "reset"), (tpl, "adc_rst"), "cpack reset bypasses pack_reset")

    # RX tagger between cpack and the RX DMA
    same((adp, "clk"), (cpack, "clk"), "adapter clock")
    same((adp, "rst"), (tpl, "adc_rst"), "adapter reset")
    for a_port, c_port in (
        ("s_fifo_wr_en", "packed_fifo_wr_en"),
        ("s_fifo_wr_data", "packed_fifo_wr_data"),
        ("s_fifo_wr_sync", "packed_sync"),
        ("s_fifo_wr_overflow", "packed_fifo_wr_overflow"),
    ):
        same((adp, a_port), (cpack, c_port), "cpack -> adapter")
    for a_port, d_port in (
        ("m_fifo_wr_en", "fifo_wr_en"),
        ("m_fifo_wr_data", "fifo_wr_din"),
        ("m_fifo_wr_sync", "sync"),
        ("m_fifo_wr_overflow", "fifo_wr_overflow"),
    ):
        same((adp, a_port), (dma, d_port), "adapter -> RX DMA")
    differ((dma, "fifo_wr_din"), (cpack, "packed_fifo_wr_data"), "RX DMA bypasses the tagger")
    differ((dma, "fifo_wr_en"), (cpack, "packed_fifo_wr_en"), "RX DMA bypasses the tagger")
    same((adp, "pass_dev"), (rxg, "pass_dev"), "adapter pass from the RX gate")
    same((adp, "mode_dev"), (rxg, "mode_dev"), "adapter mode from the RX gate")
    same((adp, "tag_clk"), (sched, "s_axi_aclk"), "adapter tag clock")
    for f in ("ticks", "seq", "target", "command", "gate", "count", "late", "valid"):
        same((adp, f"tag_{f}"), (sched, f"capture_tag_{f}"), "capture tag")
    same((adp, "tag_oneshot"), (sched, "stream_tag_oneshot"), "tagger oneshot")
    same((adp, "tag_enable"), ("tick_tag_en_slice", "Dout"), "tagger enable")
    same(("tick_tag_en_slice", "Din"), (sched, "stream_tag_en"), "tagger enable source")
    same((adp, "dbg_status"), (sched, "stream_tag_probe"), "tagger diagnostics")

    # TX gate between upack and the TX TPL
    same((txg, "tx_rd_en_in"), (ttpl, "dac_valid_0"), "TX gate input")
    same((upack, "fifo_rd_en"), (txg, "tx_rd_en_out"), "upack fifo_rd_en driven by the TX gate")
    differ((upack, "fifo_rd_en"), (ttpl, "dac_valid_0"), "upack fifo_rd_en bypasses the TX gate")
    same((txg, "dev_rst"), (ttpl, "dac_rst"), "TX gate reset")
    same((txg, "tx_data_in"), ("tick_tx_gate_data_concat", "dout"), "TX gate data in")
    for i in range(8):
        same(("tick_tx_gate_data_concat", f"In{i}"), (upack, f"fifo_rd_data_{i}"), "TX lane in")
        same((f"tick_tx_gate_data_slice_{i}", "Din"), (txg, "tx_data_out"), "TX lane slice")
        same((ttpl, f"dac_data_{i}"), (f"tick_tx_gate_data_slice_{i}", "Dout"), "TX lane out")
        differ((ttpl, f"dac_data_{i}"), (upack, f"fifo_rd_data_{i}"), "TX lane bypasses gate")

    # Tick on the converter clock (P3b option 1): the scheduler shares upack's core_clk_a, and
    # differs from the hop controller's sys_cpu_clk, which gets the FHM GPIO through
    # tick_hop_gpio_sync instead of straight from Tick's slice.
    hop, gsync = "vrt49_fhm_hop_ctrl_0", "tick_hop_gpio_sync"
    same((sched, "s_axi_aclk"), (upack, "clk"), "Tick timebase on core_clk_a")
    differ((sched, "s_axi_aclk"), (hop, "clk"), "Tick timebase on the hop controller's clock")
    same((gsync, "gpio_async"), ("tick_fhm_gpio_slice", "Dout"), "hop GPIO sync input")
    same((hop, "scheduler_gpio"), (gsync, "gpio_sync"), "hop controller GPIO through the sync")
    same((gsync, "clk"), (hop, "clk"), "hop GPIO sync clock")
    differ((hop, "scheduler_gpio"), ("tick_fhm_gpio_slice", "Dout"), "hop GPIO bypasses the sync")

    # the stock TX DMA -> axi_tx_fifo path (VRT_DAC_TX_ENABLE=0)
    if "vrt49_dac_tx_0" in insts:
        errors.append("vrt49_dac_tx_0 is instantiated: the build did not get VRT_DAC_TX_ENABLE=0")
    for d_port, f_port in (
        ("m_axis_data", "dma_data"),
        ("m_axis_valid", "dma_valid"),
        ("m_axis_ready", "dma_ready"),
        ("m_axis_last", "dma_xfer_last"),
        ("m_axis_xfer_req", "dma_xfer_req"),
    ):
        same(("axi_adrv9009_som_tx_dma", d_port), ("axi_tx_fifo", f_port), "stock TX DMA")
    return errors


_ASSIGN_CONCAT = re.compile(r"^\s*assign\s+(\w+)\s*=\s*\{([^{}]*)\};", re.M)


def concat_sources(netlist: str, insts, net: str) -> list[str] | None:
    """MSB-first sources of an xlconcat output net: Vivado inlines xlconcat as
    ``assign <cell>_dout = {InN, ..., In0};``; an instantiated concat is read by its pins."""
    m = re.search(r"^module system\b.*?^endmodule", netlist, re.M | re.S)
    for am in _ASSIGN_CONCAT.finditer(m.group(0) if m else ""):
        if am.group(1) == net:
            return [re.sub(r"\s+", "", s) for s in am.group(2).split(",")]
    for kind, conns in insts.values():
        # A block-design xlconcat instance is named system_<cell>_0 in the synth
        # netlist, so recognise it by its pins (In0..InN, dout) as well as by kind.
        concat_shaped = set(conns) - {"dout"} and all(
            re.fullmatch(r"In\d+", p) for p in set(conns) - {"dout"}
        )
        if ("xlconcat" in kind or concat_shaped) and conns.get("dout") == net:
            ins = sorted((int(p[2:]), n) for p, n in conns.items() if re.fullmatch(r"In\d+", p))
            return [n for _, n in reversed(ins)]
    return None


def check_adrv9361(netlist: str) -> list[str]:
    """Net-level assertions for hdl/system_bd_adrv9361_hop.tcl (see the module docstring)."""
    insts = top_instances(netlist)
    errors: list[str] = []

    def pin(inst: str, port: str) -> str | None:
        if inst not in insts:
            errors.append(f"instance {inst} missing from module system")
            return None
        net = insts[inst][1].get(port)
        if not net or _LITERAL.fullmatch(net):
            errors.append(f"{inst}.{port} is unconnected or tied off ({net})")
            return None
        return net

    def same(a: tuple[str, str], b: tuple[str, str], why: str) -> None:
        na, nb = pin(*a), pin(*b)
        if na is not None and nb is not None and na != nb:
            errors.append(f"{why}: {a[0]}.{a[1]} ({na}) != {b[0]}.{b[1]} ({nb})")

    def differ(a: tuple[str, str], b: tuple[str, str], why: str) -> None:
        na, nb = pin(*a), pin(*b)
        if na is not None and na == nb:
            errors.append(f"{why}: {a[0]}.{a[1]} and {b[0]}.{b[1]} share {na}")

    sched, hop, gsync = (
        "axi_timed_command_scheduler_0",
        "vrt49_fhm_hop_ctrl_0",
        "tick_hop_gpio_sync",
    )
    ev, sl, pg = "tick_hop_event_slice", "tick_hop_slot_slice", "tick_pps_gen_0"
    tb, fr, tap = "vrt49_time_base_0", "axi_vrt49_framer_0", "vrt49_rx_tap"

    for name, (kind, _) in insts.items():
        if "vrt49_ad9361_hop_ctrl" in kind or name in ("tick_scheduler_0", "tick_pps_tieoff"):
            errors.append(f"{name} ({kind}) must not be in the adrv9361 design")

    # CTRL_IN[3:1] = gpio_out[2:0] on the tick_ctrl_in port (system_top.v's IOB registers)
    same(("tick_ctrl_in_slice", "Din"), (sched, "gpio_out"), "CTRL_IN slice source")
    if insts.get("tick_ctrl_in_slice", ("", {}))[1].get("Dout") != "tick_ctrl_in":
        errors.append("tick_ctrl_in_slice.Dout does not drive the tick_ctrl_in port")

    # HOP2 scheduler_gpio = {gpio_out[2:0], gpio_out[3]} through tick_hop_gpio_sync
    same((ev, "Din"), (sched, "gpio_out"), "event slice source")
    same((sl, "Din"), (sched, "gpio_out"), "slot slice source")
    gin = pin(gsync, "gpio_async")
    if gin is not None:
        srcs = concat_sources(netlist, insts, gin)
        want = [insts.get(sl, ("", {}))[1].get("Dout"), insts.get(ev, ("", {}))[1].get("Dout")]
        if srcs is None:
            errors.append(f"{gsync}.gpio_async ({gin}) is not an xlconcat output")
        elif srcs != want:
            errors.append(
                f"{gsync}.gpio_async = {{{', '.join(srcs)}}}, expected {{{', '.join(map(str, want))}}}"
                " (slot bits 3:1, event bit 0)"
            )
    same((hop, "scheduler_gpio"), (gsync, "gpio_sync"), "HOP2 GPIO through the sync")
    same((gsync, "clk"), (hop, "clk"), "GPIO sync on the controller clock")
    differ((hop, "clk"), (sched, "s_axi_aclk"), "HOP2 on Tick's clock")
    differ((hop, "scheduler_gpio"), (sched, "gpio_out"), "HOP2 GPIO bypasses the sync")
    for h_port, f_port in (
        ("stream_enable", "fabric_sample_enable"),
        ("fabric_samples_drained", "fabric_samples_drained"),
        ("fabric_context_valid", "fabric_context_valid"),
        ("fabric_context_ready", "fabric_context_ready"),
        ("fabric_rf_frequency_q44_20", "fabric_rf_frequency_q44_20"),
        ("fabric_hop_epoch", "fabric_hop_epoch"),
        ("fabric_context_accepted", "fabric_context_accepted"),
    ):
        same((hop, h_port), (fr, f_port), "HOP2 -> framer fabric net")

    # one internal 1PPS on Tick's clock for Tick and the VRT time base
    same((pg, "pps_out"), (sched, "pps_in"), "Tick pps_in from tick_pps_gen")
    same((pg, "pps_out"), (tb, "pps_in"), "time base pps_in from tick_pps_gen")
    same((pg, "clk"), (sched, "s_axi_aclk"), "tick_pps_gen on Tick's clock")
    same((tb, "divclk"), (tap, "clk"), "time base on the tap's clock")
    # the time base counts the tap's forwarded beats: tap_accept on the tap's input + drops
    same(("tap_accept_0", "sample_valid"), (tap, "sample_valid"), "tap_accept sees the tap input")
    same(
        ("tap_accept_0", "dropped_sample_count"),
        (tap, "dropped_sample_count"),
        "tap_accept sees the tap's drop count",
    )
    same(("tap_accept_0", "clk"), (tap, "clk"), "tap_accept on the tap's clock")
    same((tb, "sample_valid"), ("tap_accept_0", "accepted"), "time base counts accepted beats")
    differ((tb, "sample_valid"), (tap, "sample_valid"), "time base not on the raw tap input")
    alt = insts.get(tb, ("", {}))[1].get("pps_alt_in")
    if not alt or not (_LITERAL.fullmatch(alt) or alt in _constant_drivers(insts)):
        errors.append(f"{tb}.pps_alt_in is not tied off ({alt})")
    for sig in ("toggle", "seconds", "count", "pps_period", "lock_state", "missed_count"):
        same((tb, f"tuple_{sig}"), (fr, f"tb_tuple_{sig}"), "time base tuple -> framer")
    for f_port, t_port in (
        ("tb_seconds_req", "cfg_seconds_req"),
        ("tb_seconds_next", "cfg_seconds_next"),
        ("tb_seconds_immediate", "cfg_seconds_immediate"),
        ("tb_nominal_sps", "cfg_nominal_sps"),
        ("tb_pps_margin", "cfg_pps_margin"),
        ("tb_pps_source", "cfg_pps_source"),
    ):
        same((fr, f_port), (tb, t_port), "framer config -> time base")
    return errors


def wrapper_text(project_dir: Path, cell: str) -> str | None:
    hits = sorted(project_dir.glob(f"**/ip/system_{cell}_0/synth/system_{cell}_0.v"))
    return hits[0].read_text(errors="replace") if hits else None


def check_a9_wrappers(project_dir: Path) -> list[str]:
    errors = []
    for cell, (module, want) in A9_WRAPPERS.items():
        got = wrapper_params(project_dir, cell)
        if got is None:
            errors.append(f"no generated wrapper (with parameters) for BD cell {cell}")
            continue
        kind, params = got
        if module not in kind:
            errors.append(f"BD cell {cell} wraps {kind}, not {module}")
        for k, v in want.items():
            if params.get(k) != v:
                errors.append(f"BD cell {cell}: {k} = {params.get(k)}, expected {v}")
    for cell, module in A9_MODULE_REFS.items():
        text = wrapper_text(project_dir, cell)
        if text is None:
            errors.append(f"no generated wrapper for BD cell {cell}")
        elif f"xilinx.com:module_ref:{module}:" not in text:
            errors.append(f"BD cell {cell} is not a module reference to {module}")
    return errors


def check_a9_gate_report(project_dir: Path) -> list[str]:
    hits = sorted(project_dir.glob(f"**/{A9_GATE_REPORT}"))
    if not hits:
        return [f"no {A9_GATE_REPORT} under {project_dir} (post-route gate did not run)"]
    lines = [ln.strip() for ln in hits[0].read_text(errors="replace").splitlines() if ln.strip()]
    errors = [f"{A9_GATE_REPORT}: {ln}" for ln in lines if " FAIL" in ln or ln.endswith("FAILED")]
    if not lines or lines[-1] != "HOPCAP_ADRV9361 ALL PASS":
        errors.append(f"{A9_GATE_REPORT} does not end with 'HOPCAP_ADRV9361 ALL PASS'")
    iob = [ln for ln in lines if ln.startswith("HOPCAP_ADRV9361 PASS CTRL_IN IOB")]
    if len(iob) != 3:
        errors.append(f"{A9_GATE_REPORT}: {len(iob)} CTRL_IN IOB PASS lines, expected 3")
    return errors


_WRAPPED = re.compile(r"^\s*(\w+)\s*#\((.*?)\)\s*inst\s*\(", re.M | re.S)
_PARAM = re.compile(r"\.(\w+)\(([^()]*)\)")


def wrapper_params(project_dir: Path, cell: str) -> tuple[str, dict[str, str]] | None:
    """(module, {param: value}) from a BD cell's generated synth wrapper, or None."""
    hits = sorted(project_dir.glob(f"**/ip/system_{cell}_0/synth/system_{cell}_0.v"))
    if not hits:
        return None
    m = _WRAPPED.search(hits[0].read_text(errors="replace"))
    if m is None:
        return None
    return m.group(1), {k: v.strip() for k, v in _PARAM.findall(m.group(2))}


def check_tdma_wrappers(project_dir: Path) -> list[str]:
    errors = []
    for cell, (module, want) in TDMA_WRAPPERS.items():
        got = wrapper_params(project_dir, cell)
        if got is None:
            errors.append(f"no generated wrapper for BD cell {cell}")
            continue
        kind, params = got
        if module not in kind:
            errors.append(f"BD cell {cell} wraps {kind}, not {module}")
        for k, v in want.items():
            if params.get(k) != v:
                errors.append(f"BD cell {cell}: {k} = {params.get(k)}, expected {v}")
    return errors


def check(project_dir: Path, top: Path, profile: str = "default") -> list[str]:
    errors: list[str] = []
    candidates = sorted(project_dir.glob("**/synth/system.v")) or sorted(
        project_dir.glob("**/system.v")
    )
    if not candidates:
        return [f"no synthesized system.v under {project_dir}"]
    netlist = candidates[0].read_text(errors="replace")
    a9 = profile == "adrv9361"
    for needle in REQUIRED_NETLIST_A9 if a9 else REQUIRED_NETLIST:
        if needle not in netlist:
            errors.append(f"netlist {candidates[0]} lacks {needle}")
    if a9:
        for needle in FORBIDDEN_NETLIST_A9:
            if needle in netlist:
                errors.append(f"netlist {candidates[0]} still has {needle}")
        errors += [f"netlist {candidates[0]}: adrv9361: {e}" for e in check_adrv9361(netlist)]
        errors += [f"adrv9361: {e}" for e in check_a9_wrappers(project_dir)]
        errors += [f"adrv9361: {e}" for e in check_a9_gate_report(project_dir)]
    else:
        errors += [f"netlist {candidates[0]}: {e}" for e in check_pps(netlist)]
    if profile == "timed-dma":
        errors += [f"netlist {candidates[0]}: timed-dma: {e}" for e in check_timed_dma(netlist)]
        errors += [f"timed-dma: {e}" for e in check_tdma_wrappers(project_dir)]
    text = top.read_text()
    required = {"timed-dma": REQUIRED_TOP_TDMA, "adrv9361": REQUIRED_TOP_A9}.get(
        profile, REQUIRED_TOP
    )
    for needle in required:
        if needle not in text:
            errors.append(f"{top} lacks {needle!r}")
    forbidden = {"timed-dma": FORBIDDEN_TOP_TDMA, "adrv9361": FORBIDDEN_TOP_A9}.get(profile, ())
    for needle in forbidden:
        if needle in text:
            errors.append(f"{top} still has {needle!r}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hdl_root", type=Path)
    ap.add_argument("--port", choices=tuple(PORT_TOP), default="qsfp")
    ap.add_argument("--profile", choices=PROFILES, default="default")
    args = ap.parse_args()
    if args.profile == "adrv9361":
        if args.port != "sfp":
            ap.error("--profile adrv9361 is an SFP design; pass --port sfp")
        project_dir, top = args.hdl_root / A9_PROJECT, args.hdl_root / A9_TOP
    else:
        project_dir = args.hdl_root / "projects/adrv9009zu11eg_vrt49_corundum"
        top = args.hdl_root / PORT_TOP[args.port]
    errors = check(project_dir, top, args.profile)
    for e in errors:
        print(f"check_hop_netlist: {e}", file=sys.stderr)
    if not errors and args.profile != "default":
        print(f"check_hop_netlist: profile {args.profile}: OK")
    print("check_hop_netlist: OK" if not errors else f"check_hop_netlist: {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
