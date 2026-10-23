# SPDX-License-Identifier: MIT
"""The hopcap overlay must compile and carry the Tick and hop-controller nodes at the
addresses the block design uses, in the 2/1-cell fpga-axi@0 convention vrt49's overlay uses."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DTS = ROOT / "kernel/dts/zynqmp-adrv9009zu11eg-hopcap-overlay.dts"
REGMAP = json.loads((ROOT / "hdl/hopctrl_regmap.json").read_text())


def test_overlay_targets_fpga_axi_with_vrt49_cell_convention():
    text = DTS.read_text()
    assert 'target-path = "/fpga-axi@0";' in text
    assert "#address-cells = <2>;" in text and "#size-cells = <1>;" in text


def test_overlay_declares_tick_and_hop_controller_at_bd_addresses():
    text = DTS.read_text()
    assert "timed-command-scheduler@86000000" in text
    assert 'compatible = "adi,axi-timed-command-scheduler-1.0";' in text
    assert "reg = <0x0 0x86000000 0x1000>;" in text
    assert "adi,time-tick-hz = <100000000>;" in text
    assert 'clock-names = "s_axi_aclk";' in text
    assert "fhm-hop-controller@84ac0000" in text
    assert 'compatible = "nous,vrt49-fhm-hop-controller-1.0";' in text
    assert "reg = <0x0 0x84ac0000 0x1000>;" in text
    # the BD fragment and the overlay must agree on the controller base
    tcl = (ROOT / "hdl/system_bd_fabric_hop.tcl").read_text()
    assert re.search(r"set hop_ctrl_base 0x84AC0000", tcl)


@pytest.mark.skipif(
    shutil.which("dtc") is None or shutil.which("fdtget") is None, reason="dtc/fdtget missing"
)
def test_overlay_compiles_and_fdtget_reads_nodes(tmp_path):
    dtbo = tmp_path / "hopcap.dtbo"
    subprocess.run(
        ["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", str(dtbo), str(DTS)],
        check=True,
        capture_output=True,
    )
    comp = subprocess.check_output(
        [
            "fdtget",
            "-t",
            "s",
            str(dtbo),
            "/fragment@0/__overlay__/timed-command-scheduler@86000000",
            "compatible",
        ],
        text=True,
    ).strip()
    assert comp == "adi,axi-timed-command-scheduler-1.0"
    hz = subprocess.check_output(
        [
            "fdtget",
            "-t",
            "i",
            str(dtbo),
            "/fragment@0/__overlay__/timed-command-scheduler@86000000",
            "adi,time-tick-hz",
        ],
        text=True,
    ).strip()
    assert hz == "100000000"
    hop = subprocess.check_output(
        [
            "fdtget",
            "-t",
            "s",
            str(dtbo),
            "/fragment@0/__overlay__/fhm-hop-controller@84ac0000",
            "compatible",
        ],
        text=True,
    ).strip()
    assert hop == "nous,vrt49-fhm-hop-controller-1.0"
