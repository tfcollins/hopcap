# SPDX-License-Identifier: MIT
"""FastLockBank against a fake AD9361 (profile RAM, the 0x25A/0x29A setup registers and the
driver's recall behaviour) and a fake board shell that executes the debugfs commands."""

from __future__ import annotations

import re

import pytest

from hopcap.fastlock import (
    PINCONTROL_ATTRS,
    REG_RX_FASTLOCK_SETUP,
    REG_TX_FASTLOCK_SETUP,
    FastLockBank,
    FastLockError,
    PhyRegs,
    decode_setup,
)
from hopcap.plan import FastLockPlan, FastLockSlot, compile_fastlock_plan

MHZ = 1_000_000
SLOTS = (
    FastLockSlot(2_400 * MHZ, 2_400 * MHZ),
    FastLockSlot(2_400 * MHZ, 2_402 * MHZ),
    FastLockSlot(2_405 * MHZ, 2_402 * MHZ),
)


class FakeAd9361:
    """What the ADI 2023_R2_P1 driver does: fastlock_store saves the current LO in profile k,
    fastlock_recall writes the setup register = profile<<5 | (pinctrl ? D1 : 0) | D0."""

    def __init__(self):
        self.lo = {"altvoltage0": 0, "altvoltage1": 0}
        self.profiles: dict[tuple[str, int], int] = {}
        self.pinctrl = {PINCONTROL_ATTRS[0]: 1, PINCONTROL_ATTRS[1]: 1}  # DT-enabled: must clear
        self.reg = {REG_RX_FASTLOCK_SETUP: 0x00, REG_TX_FASTLOCK_SETUP: 0x00, 0x123: 0xAB}
        self.log: list[tuple] = []

    def write_channel(self, channel, attr, value):
        self.log.append(("chan", channel, attr, value))
        if attr == "frequency":
            self.lo[channel] = int(value)
        elif attr == "fastlock_store":
            self.profiles[(channel, int(value))] = self.lo[channel]
        elif attr == "fastlock_recall":
            k = int(value)
            assert (channel, k) in self.profiles, "recall of an unstored profile is -EINVAL"
            tx = channel == "altvoltage1"
            pin = self.pinctrl[PINCONTROL_ATTRS[tx]]
            reg = REG_TX_FASTLOCK_SETUP if tx else REG_RX_FASTLOCK_SETUP
            self.reg[reg] = (k << 5) | (0x2 if pin else 0) | 0x1
        else:
            raise KeyError(attr)

    def write_debug(self, attr, value):
        self.log.append(("debug", attr, value))
        self.pinctrl[attr] = int(value)


class FakeDebugfsShell:
    """Interprets the board-side snippets PhyRegs sends (debugfs direct_reg_access)."""

    def __init__(self, chip: FakeAd9361, *, stuck_bits: int = 0):
        self.chip = chip
        self.stuck_bits = stuck_bits  # bits the chip refuses to set (a failed write)
        self.cmds: list[str] = []

    def run(self, cmd, *, timeout_s=15.0):
        self.cmds.append(cmd)
        assert "direct_reg_access" in cmd and "mount -t debugfs" in cmd
        out = []
        for m in re.finditer(
            r'echo 0x([0-9A-F]+) > "\$f"; v=\$\(cat "\$f"\); n=\$\(\( \(v & ~(\d+)\) \| (\d+) \)\)',
            cmd,
        ):
            reg, clear, set_ = int(m.group(1), 16), int(m.group(2)), int(m.group(3))
            before = self.chip.reg[reg]
            self.chip.reg[reg] = ((before & ~clear) | set_) & ~self.stuck_bits
            out.append(f"REG 0x{reg:X} 0x{before:02X} 0x{self.chip.reg[reg]:02X}")
        if not out:
            for m in re.finditer(r'echo 0x([0-9A-F]+) > "\$f"; echo "REG', cmd):
                reg = int(m.group(1), 16)
                out.append(f"REG 0x{reg:X} 0x{self.chip.reg[reg]:02X}")
        return "\n".join(out) + "\n"


def bank(chip=None, **kw):
    chip = chip or FakeAd9361()
    sh = FakeDebugfsShell(chip, **kw)
    return FastLockBank(chip.write_channel, chip.write_debug, PhyRegs(sh), log=lambda m: None), chip


def test_load_stores_rx_then_tx_per_slot_with_pin_control_off_then_recalls_0():
    b, chip = bank()
    rec = b.load(SLOTS)
    assert chip.log[:2] == [("debug", a, "0") for a in PINCONTROL_ATTRS]
    chans = [e[1:] for e in chip.log if e[0] == "chan"]
    expect = []
    for k, s in enumerate(SLOTS):
        expect += [
            ("altvoltage0", "frequency", str(s.rx_lo_hz)),
            ("altvoltage0", "fastlock_store", str(k)),
            ("altvoltage1", "frequency", str(s.tx_lo_hz)),
            ("altvoltage1", "fastlock_store", str(k)),
        ]
    expect += [("altvoltage0", "fastlock_recall", "0"), ("altvoltage1", "fastlock_recall", "0")]
    assert chans == expect
    for k, s in enumerate(SLOTS):
        assert chip.profiles[("altvoltage0", k)] == s.rx_lo_hz
        assert chip.profiles[("altvoltage1", k)] == s.tx_lo_hz
    assert rec["setup_after_recall"] == {"0x25A": 0x01, "0x29A": 0x01}


def test_pin_select_is_read_modify_write_of_d1_d0_with_readback():
    b, chip = bank()
    b.load(SLOTS)
    chip.reg[REG_RX_FASTLOCK_SETUP] |= 0x10  # an unrelated bit must survive
    rec = b.enable_pin_select()
    assert chip.reg[REG_RX_FASTLOCK_SETUP] == 0x13 and chip.reg[REG_TX_FASTLOCK_SETUP] == 0x03
    assert rec == {"0x25A": [0x11, 0x13], "0x29A": [0x01, 0x03]}
    assert chip.reg[0x123] == 0xAB
    assert b.readback_hz() is None  # cached LO is not evidence of a pin recall
    n_chan = sum(1 for e in chip.log if e[0] == "chan")
    b.disable()
    assert chip.reg[REG_RX_FASTLOCK_SETUP] == 0x11 and chip.reg[REG_TX_FASTLOCK_SETUP] == 0x01
    assert sum(1 for e in chip.log if e[0] == "chan") == n_chan, "no LO write or recall after"
    assert decode_setup(0xE3) == {
        "raw": 0xE3,
        "mode_enable": True,
        "pin_select": True,
        "profile": 7,
    }


def test_recall_with_driver_pin_control_left_on_fails_closed():
    """If the driver flag stayed on, recall already set D1 before Tick parked CTRL_IN."""
    chip = FakeAd9361()
    b, _ = bank(chip)
    b.write_debug = lambda attr, value: None  # the clear never reaches the driver
    with pytest.raises(FastLockError, match="pin select off"):
        b.load(SLOTS)


def test_pin_select_write_that_does_not_stick_fails():
    b, _chip = bank(stuck_bits=0x2)
    b.load(SLOTS)
    with pytest.raises(FastLockError, match=r"expected D1\|D0 set"):
        b.enable_pin_select()


def test_guards():
    b, _ = bank()
    with pytest.raises(FastLockError, match="not loaded"):
        b.enable_pin_select()
    with pytest.raises(FastLockError, match=r"1\.\.8"):
        b.load(SLOTS * 3)
    b.load(SLOTS)
    b.enable()
    with pytest.raises(FastLockError, match="disable it before"):
        b.load(SLOTS)
    with pytest.raises(FastLockError, match="nothing is staged"):
        b.stage(2_400 * MHZ)


def test_prepare_takes_the_plan_slots():
    p = FastLockPlan(slots=SLOTS, hops=(1, 2, 0), period_us=1_250_000, settle_us=0, capture_us=10)
    b, chip = bank()
    b.prepare(p)
    assert b.loaded and len(chip.profiles) == 6
    with pytest.raises(FastLockError, match="FastLock"):
        bank()[0].prepare(compile_fastlock_plan(p, t0_ticks=0, tick_hz=50_000_000).plan)


def test_board_snippets_are_valid_posix_sh():
    import subprocess

    b, _chip = bank()
    b.load(SLOTS)
    b.enable_pin_select()
    sh = b.regs.shell
    assert sh.cmds
    for cmd in sh.cmds:
        r = subprocess.run(["sh", "-n", "-c", cmd], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
