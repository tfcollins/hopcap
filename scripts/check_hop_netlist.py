#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Post-synthesis assertions for the hopcap composite build.

Reads the Vivado project's synthesized netlist (``synth/system.v`` under the project dir,
as vrt49's check-sfp-rx-netlist.py does) and the staged top-level, and fails closed unless:
  * vrt49_fhm_hop_ctrl is instantiated and its fabric nets reach axi_vrt49_framer;
  * axi_timed_command_scheduler is instantiated;
  * the top-level drives adrv9009_gpio_00_a from tick_fhm_gpio[0].
"""

from __future__ import annotations

import argparse
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
)
REQUIRED_TOP = (
    "assign adrv9009_gpio_00_a = tick_fhm_gpio[0];",
    ".tick_fhm_gpio (tick_fhm_gpio),",
    ".DATA_WIDTH(60)",
)


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
    text = top.read_text()
    for needle in REQUIRED_TOP:
        if needle not in text:
            errors.append(f"{top} lacks {needle!r}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hdl_root", type=Path)
    args = ap.parse_args()
    project_dir = args.hdl_root / "projects/adrv9009zu11eg_vrt49_corundum"
    top = args.hdl_root / "projects/adrv9009zu11eg/adrv2crr_fmc/system_top_corundum.v"
    errors = check(project_dir, top)
    for e in errors:
        print(f"check_hop_netlist: {e}", file=sys.stderr)
    print("check_hop_netlist: OK" if not errors else f"check_hop_netlist: {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
