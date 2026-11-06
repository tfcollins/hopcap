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


def check(project_dir: Path, top: Path) -> list[str]:
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
    text = top.read_text()
    for needle in REQUIRED_TOP:
        if needle not in text:
            errors.append(f"{top} lacks {needle!r}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hdl_root", type=Path)
    ap.add_argument("--port", choices=tuple(PORT_TOP), default="qsfp")
    args = ap.parse_args()
    project_dir = args.hdl_root / "projects/adrv9009zu11eg_vrt49_corundum"
    top = args.hdl_root / PORT_TOP[args.port]
    errors = check(project_dir, top)
    for e in errors:
        print(f"check_hop_netlist: {e}", file=sys.stderr)
    print("check_hop_netlist: OK" if not errors else f"check_hop_netlist: {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
