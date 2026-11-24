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


# --- timed-dma profile (libdatax P3b) ------------------------------------------------------
TDMA_DTS = ROOT / "kernel/dts/zynqmp-adrv9009zu11eg-hopcap-timed-dma-overlay.dts"
TDMA_CONTROL_DTS = ROOT / "kernel/dts/zynqmp-adrv9009zu11eg-vrt49-control-timed-dma-overlay.dts"
VRT49_CONTROL_DTS = (
    ROOT
    / "third_party/vrt49/kernel/iio-vrt49-buffer/zynqmp-adrv9009zu11eg-vrt49-control-overlay.dts"
)
# libdatax src/tick_ops.c gate roles; gate role k = Tick stream gate bit k, presence roles last.
TDMA_GATE_MAP = ("rx-dma", "tx-dma", "rx-mode", "tx-mode", "fhm-b", "tx-start-fullmask-only")
TICK_NODE = "/fragment@0/__overlay__/timed-command-scheduler@86000000"


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_timed_dma_overlay_matches_the_default_overlay_plus_the_gate_map():
    """Same Tick and hop-controller nodes as hopcap-sfp; the only addition is the map."""
    base = _strip_comments(DTS.read_text()).split("/dts-v1/;", 1)[1]
    tdma = _strip_comments(TDMA_DTS.read_text()).split("/dts-v1/;", 1)[1]
    prop = re.search(r"\n\s*adi,stream-gate-map = [^;]+;", tdma).group(0)
    assert tdma.replace(prop, "") == base
    assert "adi,stream-gate-map" not in DTS.read_text()  # hopcap-sfp advertises no gate


def test_timed_dma_gate_bits_follow_the_block_design():
    tcl = (ROOT / "hdl/system_bd_timed_dma.tcl").read_text()
    m = re.search(r"foreach \{name bit\} \{([^}]+)\}", tcl)
    pairs = m.group(1).split()
    bits = {pairs[i]: int(pairs[i + 1]) for i in range(0, len(pairs), 2)}
    role_of = {
        "tick_gate_rx_level": "rx-dma",
        "tick_gate_tx_level": "tx-dma",
        "tick_gate_rx_mode": "rx-mode",
        "tick_gate_tx_mode": "tx-mode",
    }
    assert {role_of[name]: bit for name, bit in bits.items()} == {
        role: k for k, role in enumerate(TDMA_GATE_MAP[:4])
    }
    # fhm-b: the assembler routes tick_fhm_gpio[1] to chip B's TAL_GPIO_0.
    asm = (ROOT / "scripts/assemble_workspace.py").read_text()
    assert "assign adrv9009_gpio_00_b = tick_fhm_gpio[1];" in asm


def test_timed_dma_control_overlay_drops_only_the_deframer():
    adapted = TDMA_CONTROL_DTS.read_text()
    upstream = VRT49_CONTROL_DTS.read_text()
    assert adapted.splitlines()[0] == "// SPDX-License-Identifier: GPL-2.0"
    body = adapted.split("/dts-v1/;", 1)[1]
    assert "deframer" not in body
    assert "tx_dma" not in body and "dmas" not in body
    deframer = re.search(r"\n\n\s*iio_vrt49_deframer@84ab0000 \{.*?\n\s*\};", upstream, re.S)
    assert deframer is not None
    assert upstream.split("/dts-v1/;", 1)[1].replace(deframer.group(0), "") == body


@pytest.mark.skipif(
    shutil.which("dtc") is None or shutil.which("fdtget") is None, reason="dtc/fdtget missing"
)
def test_timed_dma_overlays_compile_and_carry_the_map(tmp_path):
    dtbo = tmp_path / "hopcap-timed-dma.dtbo"
    subprocess.run(
        ["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", str(dtbo), str(TDMA_DTS)],
        check=True,
        capture_output=True,
    )
    got = subprocess.check_output(
        ["fdtget", "-t", "s", str(dtbo), TICK_NODE, "adi,stream-gate-map"], text=True
    ).split()
    assert tuple(got) == TDMA_GATE_MAP
    # the Tick driver joins the list with commas; that string is what libdatax parses
    assert ",".join(got) == "rx-dma,tx-dma,rx-mode,tx-mode,fhm-b,tx-start-fullmask-only"

    ctl = tmp_path / "control-timed-dma.dtbo"
    subprocess.run(
        ["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", str(ctl), str(TDMA_CONTROL_DTS)],
        check=True,
        capture_output=True,
    )
    nodes = subprocess.check_output(
        ["fdtget", "-l", str(ctl), "/fragment@0/__overlay__"], text=True
    ).split()
    assert nodes == ["iio_vrt49_framer@84a80000"]
    hog = subprocess.check_output(
        ["fdtget", "-l", str(ctl), "/fragment@1/__overlay__"], text=True
    ).split()
    assert hog == ["vrt49-external-pps-hog"]


def test_boot_script_selects_the_timed_dma_overlays_and_skips_the_dma_owner_patch():
    script = (ROOT / "scripts/build_boot_artifacts.sh").read_text()
    tdma = script[script.index("  timed-dma)\n") : script.index('  *) echo "--profile must be')]
    assert "zynqmp-adrv9009zu11eg-vrt49-control-timed-dma-overlay.dts" in tdma
    assert "zynqmp-adrv9009zu11eg-hopcap-timed-dma-overlay.dts" in tdma
    assert '[ "$port" = sfp ]' in tdma
    patch = script.index('python3 "$vrt49/kernel/iio-vrt49-buffer/patch_adrv9009_tx_dma_owner.py"')
    guard = script.rindex('if [ "$profile" = default ]; then', 0, patch)
    assert "\nfi\n" not in script[guard:patch]
    assert f'TDMA_GATE_MAP="{" ".join(TDMA_GATE_MAP)}"' in script
    for check in ("deframer_absent", "tx_dma_stock", "tick_gate_map"):
        assert f"chk {check} " in script
    assert "scripts/extract_jtag_inputs.py" in script
