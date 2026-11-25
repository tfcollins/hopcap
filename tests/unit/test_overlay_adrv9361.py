# SPDX-License-Identifier: MIT
"""The adrv9361 runtime overlay: compiles with dtc -@, applies to a Kuiper-shaped fpga_axi
tree, carries the framer/udp, Tick, HOP2 and time-base values the block design and vrt49's
driver expect."""

from __future__ import annotations

import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DTS = ROOT / "kernel/dts/zynq-adrv9361z7035-hopcap-overlay.dts"
VRT49_OVERLAY = (
    ROOT
    / "third_party/vrt49/kernel/iio-vrt49-buffer/zynq-adrv9361z7035-vrt49-sfp-control-overlay.dts"
)
TICK_OVERLAY = ROOT / "third_party/tick/kernel/dts/zynq-adrv9361z7035-tick-scheduler.dtso"
DRIVER = ROOT / "third_party/vrt49/kernel/iio-vrt49-buffer/iio-vrt49-buffer.c"
TEXT = DTS.read_text()
FRAGMENT = (ROOT / "hdl/system_bd_adrv9361_hop.tcl").read_text()
TICK_FRAGMENT = (
    ROOT
    / "third_party/tick/hdl/projects/adrv9361z7035/ccfmc_lvds/tick_adrv9361z7035_ccfmc_lvds.tcl"
).read_text()

NOMINAL_SPS = 15_360_000


def _node(name: str) -> str:
    m = re.search(rf"^\t{re.escape(name)} \{{\n(.*?)^\t\}};", TEXT, re.M | re.S)
    assert m, name
    return m.group(1)


def test_overlay_targets_kuiper_fpga_axi_with_single_cells():
    assert "&fpga_axi {" in TEXT
    assert "#address-cells = <1>;" in TEXT and "#size-cells = <1>;" in TEXT
    assert TEXT.splitlines()[0] == "// SPDX-License-Identifier: GPL-2.0"


def test_framer_node_is_vrt49s_sfp_control_node():
    node = _node("iio_vrt49_framer@44a80000")
    vrt = VRT49_OVERLAY.read_text()
    for line in (
        'reg-names = "framer", "udp";',
        "reg = <0x44a80000 0x1000>, <0x44a90000 0x1000>;",
        "adi,stream-id = <0x01000000>;",
        "adi,packet-mtu = <1024>;",
    ):
        assert line in node, line
        assert line in vrt, line
    assert "dmas" not in node  # the DMA-less control-device path of the driver
    assert "adi,enable-at-boot" not in node.replace("No adi,enable-at-boot", "")
    # BD apertures
    bd = (ROOT / "third_party/vrt49/hdl/projects/adrv9361z7035_vrt49_sfp/system_bd.tcl").read_text()
    assert "ad_cpu_interconnect 0x44A80000 axi_vrt49_framer_0" in bd
    assert "ad_cpu_interconnect 0x44A90000 vrt49_udp_tx_0" in bd


def test_time_base_properties():
    node = _node("iio_vrt49_framer@44a80000")
    assert 'adi,pps-source-names = "tick", "disabled";' in node
    assert f"adi,nominal-sps = <{NOMINAL_SPS}>;" in node
    q32 = round(Fraction(10**12, NOMINAL_SPS) * 2**32)
    assert q32 == 279_620_266_666_667
    assert f"adi,ps-per-sample-q32 = /bits/ 64 <{q32}>;" in node
    margin = int(re.search(r"adi,pps-margin = <(\d+)>;", node).group(1))
    # two independent references (PS crystal vs AD9361 reference): at least 500 ppm, and well
    # below a sample-rate change (the lock window must still reject a wrong profile)
    assert NOMINAL_SPS * 500 // 10**6 <= margin <= NOMINAL_SPS // 100
    assert "adi,timebase-strict;" in node and "adi,timebase-start-disabled;" in node


def test_time_base_property_names_are_the_drivers():
    drv = DRIVER.read_text()
    node = _node("iio_vrt49_framer@44a80000")
    for prop in re.findall(r"(adi,[a-z0-9-]+)", node):
        if prop in ("adi,axi-vrt49-framer-1", "adi,stream-id", "adi,packet-mtu"):
            continue
        assert f'"{prop}"' in drv, prop
    # exactly two names, and the hopcap host selects the first
    assert "adi,pps-source-names must contain exactly two names" in drv


def test_tick_node_matches_ticks_z7035_overlay_and_fragment():
    node = _node("timed-command-scheduler@44aa0000")
    tick = TICK_OVERLAY.read_text()
    for line in (
        'compatible = "adi,axi-timed-command-scheduler-1.0";',
        "reg = <0x44aa0000 0x1000>;",
        "clocks = <&tick_clk>;",
        "adi,time-tick-hz = <50000000>;",
    ):
        assert line in node and line in tick, line
    clk = _node("tick_clk: tick-clk")
    for line in ("clocks = <&clkc 15>;", "clock-div = <2>;", "clock-mult = <1>;"):
        assert line in clk and line in tick, line
    assert "ad_cpu_interconnect 0x44AA0000 axi_timed_command_scheduler_0" in TICK_FRAGMENT
    assert "set tick_time_tick_hz 50000000" in TICK_FRAGMENT


def test_hop2_node_matches_the_fragment():
    node = _node("fhm-hop-controller@44ab0000")
    assert 'compatible = "nous,vrt49-fhm-hop-controller-1.0";' in node
    assert "reg = <0x44ab0000 0x1000>;" in node
    assert "adi,event-gpio-bit = <3>;" in node and "adi,slot-bits = <3>;" in node
    assert "adi,clock-hz = <100000000>;" in node
    assert "set hop_ctrl_base 0x44AB0000" in FRAGMENT
    # HOP1's compatible is gone
    assert "vrt49-ad9361-hop-controller" not in TEXT


@pytest.mark.skipif(
    shutil.which("dtc") is None
    or shutil.which("fdtoverlay") is None
    or shutil.which("fdtget") is None,
    reason="dtc/fdtoverlay/fdtget missing",
)
def test_overlay_compiles_and_applies_to_a_kuiper_shaped_base(tmp_path):
    dtbo = tmp_path / "hopcap.dtbo"
    r = subprocess.run(
        ["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", str(dtbo), str(DTS)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "Warning" not in r.stderr, r.stderr
    base = tmp_path / "base.dts"
    base.write_text(
        "/dts-v1/;\n/ {\n\t#address-cells = <1>;\n\t#size-cells = <1>;\n"
        "\tclkc: clkc@100 {\n\t\treg = <0x100 0x100>;\n\t\t#clock-cells = <1>;\n\t};\n"
        '\tfpga_axi: fpga-axi@0 {\n\t\tcompatible = "simple-bus";\n'
        "\t\t#address-cells = <1>;\n\t\t#size-cells = <1>;\n\t\tranges;\n\t};\n};\n"
    )
    base_dtb = tmp_path / "base.dtb"
    subprocess.run(
        ["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", str(base_dtb), str(base)],
        check=True,
        capture_output=True,
    )
    out = tmp_path / "merged.dtb"
    subprocess.run(
        ["fdtoverlay", "-i", str(base_dtb), "-o", str(out), str(dtbo)],
        check=True,
        capture_output=True,
    )

    def get(path: str, prop: str, kind: str = "s") -> str:
        return subprocess.check_output(
            ["fdtget", "-t", kind, str(out), path, prop], text=True
        ).strip()

    assert get("/fpga-axi@0/iio_vrt49_framer@44a80000", "adi,nominal-sps", "u") == str(NOMINAL_SPS)
    assert get("/fpga-axi@0/iio_vrt49_framer@44a80000", "adi,pps-source-names") == "tick disabled"
    assert (
        get("/fpga-axi@0/timed-command-scheduler@44aa0000", "adi,time-tick-hz", "u") == "50000000"
    )
    assert get("/fpga-axi@0/fhm-hop-controller@44ab0000", "compatible") == (
        "nous,vrt49-fhm-hop-controller-1.0"
    )
    assert get("/fpga-axi@0/tick-clk", "clock-div", "u") == "2"
