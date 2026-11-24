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
PROFILES = ("default", "timed-dma")
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
    for needle in REQUIRED_NETLIST:
        if needle not in netlist:
            errors.append(f"netlist {candidates[0]} lacks {needle}")
    errors += [f"netlist {candidates[0]}: {e}" for e in check_pps(netlist)]
    if profile == "timed-dma":
        errors += [f"netlist {candidates[0]}: timed-dma: {e}" for e in check_timed_dma(netlist)]
        errors += [f"timed-dma: {e}" for e in check_tdma_wrappers(project_dir)]
    text = top.read_text()
    for needle in REQUIRED_TOP_TDMA if profile == "timed-dma" else REQUIRED_TOP:
        if needle not in text:
            errors.append(f"{top} lacks {needle!r}")
    for needle in FORBIDDEN_TOP_TDMA if profile == "timed-dma" else ():
        if needle in text:
            errors.append(f"{top} still has {needle!r} (chip-B IOBUF split not applied)")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hdl_root", type=Path)
    ap.add_argument("--port", choices=tuple(PORT_TOP), default="qsfp")
    ap.add_argument("--profile", choices=PROFILES, default="default")
    args = ap.parse_args()
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
