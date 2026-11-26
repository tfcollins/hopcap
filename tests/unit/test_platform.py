# SPDX-License-Identifier: MIT
"""hopcap.platform: per-board constants, the Tick gpio bit map and the Tick/controller units."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tick import Flag
from tick.hdl_model import AxiTimedCommandSchedulerModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import FakeMmio

from hopcap import link, platform
from hopcap.descriptors import descriptors_for_edge
from hopcap.hopctrl import HopController, HopCtrlError, HopCtrlRegs
from hopcap.plan import Edge, HopPlan, SlotTable
from hopcap.platform import ADRV9361, ADRV9361_BITS, ZU11EG, ZU11EG_BITS, PlatformError

MHZ = 1_000_000


def test_units_convert_at_each_consumer():
    assert platform.us_to_ticks(20, 50_000_000) == 1_000
    assert platform.us_to_ticks(20, 100_000_000) == 2_000
    # identity on the ZU11EG (Tick on the controller clock), x2 on the ADRV9361
    assert platform.controller_cycles(1_234, tick_hz=100_000_000) == 1_234
    assert platform.controller_cycles(1_234, tick_hz=50_000_000) == 2_468
    # rounded up: a settle is never shortened (timed-dma 122.88 MHz Tick)
    assert platform.controller_cycles(3, tick_hz=122_880_000) == 3
    assert platform.controller_cycles(123, tick_hz=122_880_000) == 101
    with pytest.raises(PlatformError):
        platform.controller_cycles(-1, tick_hz=50_000_000)


def test_controller_clock_comes_from_the_regmap():
    assert platform.CONTROLLER_HZ == HopCtrlRegs.from_json().clock_hz == 100_000_000
    assert ADRV9361.controller_hz == ZU11EG.controller_hz == 100_000_000


def test_adrv9361_constants():
    assert ADRV9361.tick_hz == 50_000_000 and ZU11EG.tick_hz == 100_000_000
    assert ADRV9361.hopctrl_base == 0x44AB0000 and ADRV9361.framer_base == 0x44A80000
    assert ADRV9361.place == "lablp" and ADRV9361.part == "adrv9361z7035"
    assert ADRV9361.carrier == "adrv1crr-fmc"
    assert ADRV9361.phy_iio_name == "ad9361-phy" and ADRV9361.rx_iio_name == "cf-ad9361-lpc"
    assert ADRV9361.pps_source == "tick" and ADRV9361.pps_source in ADRV9361.pps_sources
    assert ZU11EG.pps_source == "corundum" and ZU11EG.hopctrl_base == 0x84AC0000
    assert platform.get("adrv9361") is ADRV9361 and platform.get() is ZU11EG
    assert platform.from_env({"HOPCAP_PLATFORM": "adrv9361"}) is ADRV9361
    with pytest.raises(PlatformError, match="unknown platform"):
        platform.get("pluto")


def test_check_tick_hz_fails_closed():
    assert ADRV9361.check_tick_hz(50_000_000) == 50_000_000
    with pytest.raises(PlatformError, match="expected 50000000"):
        ADRV9361.check_tick_hz(100_000_000)


def test_adrv9361_receiver_is_nemo_enp7s0_whatever_the_port_variable(monkeypatch):
    monkeypatch.setenv("VRT49_PHYSICAL_PORT", "qsfp")
    rx = ADRV9361.receiver()
    assert (rx.interface, rx.mac, rx.ip, rx.ipv4) == (
        "enp7s0",
        "68:05:ca:19:88:8c",
        "192.168.2.10",
        "192.168.2.10/24",
    )
    assert ZU11EG.receiver().interface == "enp4s0f0np0"  # still follows the variable
    assert link.receiver("sfp").ipv4 == "192.0.2.2/24"
    with pytest.raises(link.PortError):
        link.platform_receiver("zu11eg")


def test_bit_maps():
    # ZU11EG: unchanged (event bit 0, slot 3:1)
    assert ZU11EG_BITS.rise(5) == (0xF, 0b1011) and ZU11EG_BITS.fall() == (0x1, 0)
    # ADRV9361: CTRL_IN code = slot on bits 2:0, event on bit 3
    for k in range(8):
        assert ADRV9361_BITS.rise(k) == (0xF, 0x8 | k)
    assert ADRV9361_BITS.fall() == (0x8, 0)
    assert ADRV9361_BITS.park() == ZU11EG_BITS.park() == (0xF, 0)
    with pytest.raises(ValueError):
        ADRV9361_BITS.value(8, True)


def test_adrv9361_descriptors_drive_ctrl_in_and_event_on_the_tick_model():
    """The rise lands the CTRL_IN code and the event on the same tick; the fall clears only the
    event, so CTRL_IN keeps the profile between hops."""
    edge = Edge(
        index=0,
        commits_hz=2_400 * MHZ,
        stage_before_hz=None,
        slot=5,
        rise_ticks=1_000,
        fall_ticks=1_500,
        priming=False,
    )
    rise, fall = descriptors_for_edge(edge, ADRV9361_BITS)
    assert rise.args == (0xF, 0xD, 0, 0) and fall.args == (0x8, 0, 0, 0)
    assert rise.flags == Flag.REPORT_COMPLETION | Flag.LATE_DROP and fall.flags == Flag.LATE_DROP
    m = AxiTimedCommandSchedulerModel(queue_depth=16, tick_hz=50_000_000)
    for d in (rise, fall):
        m.push_gpio(
            d.sequence_id,
            d.timestamp_ticks,
            d.args[0],
            d.args[1],
            late_policy="drop",
            report_completion=bool(d.flags & Flag.REPORT_COMPLETION),
        )
    m.advance_to(1_000)
    assert m.gpio_out == 0xD
    m.ack_completion()
    m.advance_to(1_500)
    assert m.gpio_out == 0x5


def test_program_converts_tick_ticks_to_controller_cycles():
    regs = HopCtrlRegs.from_json()
    plan = HopPlan(
        frequencies_hz=(2_410 * MHZ,),
        period_ticks=1_000_000,
        settle_ticks=1_000,  # 20 us at 50 MHz
        capture_ticks=100_000,  # 2 ms at 50 MHz
        pulse_ticks=1_000,
        lead_ticks=1_000,
        context_margin_ticks=100,
    )
    m = FakeMmio(regs)
    HopController(m, regs).program(
        plan, SlotTable.from_plan(plan), tick_hz=50_000_000, drain_timeout_ticks=7
    )
    w = dict(m.writes)
    assert w[regs.SETTLE_TICKS] == 2_000  # 20 us of the 100 MHz controller clock
    assert w[regs.CAPTURE_TICKS] == 200_000
    assert w[regs.DRAIN_TIMEOUT_TICKS] == 14
    m2 = FakeMmio(regs)
    HopController(m2, regs).program(plan, SlotTable.from_plan(plan), tick_hz=100_000_000)
    assert dict(m2.writes)[regs.SETTLE_TICKS] == 1_000  # ZU11EG: unchanged
    big = HopPlan(
        frequencies_hz=(2_410 * MHZ,),
        period_ticks=1 << 40,
        settle_ticks=1 << 31,
        capture_ticks=1,
    )
    with pytest.raises(HopCtrlError, match="32 bits"):
        HopController(FakeMmio(regs), regs).program(
            big, SlotTable.from_plan(big), tick_hz=50_000_000
        )
