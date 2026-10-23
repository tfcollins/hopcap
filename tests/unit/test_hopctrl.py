# SPDX-License-Identifier: MIT
"""Hop-controller programmer against a register model derived from hdl/hopctrl_regmap.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hopcap.hopctrl import (
    HopController,
    HopCtrlError,
    HopCtrlRegs,
    Status,
)
from hopcap.plan import HopPlan, SlotTable, hz_to_q44_20

ROOT = Path(__file__).resolve().parents[2]
REGMAP = json.loads((ROOT / "hdl/hopctrl_regmap.json").read_text())
MHZ = 1_000_000


class FakeMmio:
    """Register-level fake: RW registers store, RO registers are test-settable."""

    def __init__(self, regs: HopCtrlRegs):
        self.r = regs
        self.mem: dict[int, int] = {
            regs.ID: regs.id_word,
            regs.VERSION: regs.version_word,
            regs.SETTLE_TICKS: 0x7D0,
            regs.SEQ_SLOT_COUNT: 8,
        }
        self.writes: list[tuple[int, int]] = []
        self.state = 0
        self.enabled = False
        self.idle_open = True
        self.slot_source = False
        self.superseded_sticky = False
        self.drain_timeout_sticky = False

    def read32(self, off: int) -> int:
        if off == self.r.CTRL_STATUS:
            gate = (
                1
                if (not self.enabled or (self.state == 0 and self.idle_open) or self.state == 4)
                else 0
            )
            return (
                self.state
                | gate << 3
                | self.superseded_sticky << 4
                | self.drain_timeout_sticky << 5
                | self.enabled << 8
                | self.slot_source << 9
                | self.idle_open << 11
            )
        return self.mem.get(off, 0)

    def write32(self, off: int, value: int) -> None:
        self.writes.append((off, value))
        if off == self.r.CTRL_STATUS:
            self.enabled = bool(value & 1)
            self.slot_source = bool(value & 2)
            self.idle_open = bool(value & 8)
            if value & 4:
                self.superseded_sticky = self.drain_timeout_sticky = False
                self.mem[self.r.SUPERSEDED_COUNT] = 0
            return
        self.mem[off] = value


@pytest.fixture
def regs() -> HopCtrlRegs:
    return HopCtrlRegs.from_json(ROOT / "hdl/hopctrl_regmap.json")


def test_regs_expose_every_offset_from_json(regs):
    for r in REGMAP["registers"]:
        assert getattr(regs, r["name"]) == int(r["offset"], 16)
    assert regs.id_word == int(REGMAP["id_word"], 16)
    assert regs.slot_lo(3) == 0x58 and regs.slot_hi(3) == 0x5C
    assert regs.states["OPEN"] == 4


def test_identify_rejects_wrong_id(regs):
    m = FakeMmio(regs)
    m.mem[regs.ID] = 0x484F5031  # the AD9361 bridge's "HOP1"
    with pytest.raises(HopCtrlError, match="HOP1"):
        HopController(m, regs).identify()


def test_program_writes_slots_settle_capture_then_enables(regs):
    m = FakeMmio(regs)
    hc = HopController(m, regs)
    plan = HopPlan(
        frequencies_hz=(2_410 * MHZ, 2_420 * MHZ),
        period_ticks=10_000_000,
        settle_ticks=12_345,
        capture_ticks=67_890,
    )
    slots = SlotTable.from_plan(plan)
    hc.program(plan, slots)
    # every slot word is written lo then hi, unused slots zeroed
    words = slots.words()
    for i in range(8):
        assert m.mem[regs.slot_lo(i)] == words[i] & 0xFFFFFFFF
        assert m.mem[regs.slot_hi(i)] == words[i] >> 32
    assert m.mem[regs.slot_lo(0)] == (hz_to_q44_20(2_450 * MHZ)) & 0xFFFFFFFF
    assert m.mem[regs.SETTLE_TICKS] == 12_345
    assert m.mem[regs.CAPTURE_TICKS] == 67_890
    # CTRL written last, with enable + idle_open, and the stickies cleared
    last_off, last_val = m.writes[-1]
    assert last_off == regs.CTRL_STATUS
    assert last_val & 0b1 and last_val & 0b1000 and last_val & 0b100
    assert m.enabled and m.idle_open and not m.slot_source


def test_status_decodes_fields(regs):
    m = FakeMmio(regs)
    m.enabled = True
    m.state = 3
    m.superseded_sticky = True
    m.mem[regs.EPOCH] = 7
    m.mem[regs.EVENT_COUNT] = 7
    m.mem[regs.SUPERSEDED_COUNT] = 1
    st: Status = HopController(m, regs).status()
    assert st.state_name == "PUBLISH" and st.enabled and st.superseded_sticky
    assert st.epoch == 7 and st.event_count == 7 and st.superseded_count == 1
    assert st.gate_open is False


def test_require_idle_fails_closed_on_sticky_or_busy(regs):
    m = FakeMmio(regs)
    hc = HopController(m, regs)
    m.enabled = True
    hc.require_idle()  # IDLE, clean
    m.state = 4  # OPEN with capture_ticks=0 is "idle enough"? no: fail
    with pytest.raises(HopCtrlError, match="state"):
        hc.require_idle()
    m.state = 0
    m.drain_timeout_sticky = True
    with pytest.raises(HopCtrlError, match="drain"):
        hc.require_idle()


def test_wait_drained_requires_epoch_advance_and_quiescence(regs):
    m = FakeMmio(regs)
    hc = HopController(m, regs)
    m.enabled = True
    m.mem[regs.EPOCH] = 4
    m.state = 5  # CLOSED after the last capture
    hc.wait_drained(expected_events=4, timeout_s=0.01, sleep=lambda s: None)
    m.mem[regs.EPOCH] = 3
    with pytest.raises(HopCtrlError, match="epoch"):
        hc.wait_drained(expected_events=4, timeout_s=0.01, sleep=lambda s: None)


def test_disable_keeps_gate_open(regs):
    m = FakeMmio(regs)
    hc = HopController(m, regs)
    hc.disable()
    assert not m.enabled and m.idle_open
    assert hc.status().gate_open
