# SPDX-License-Identifier: MIT
"""AD9361 FastLock bank for the ADRV9361 hop (replaces the Talise stager on that platform).

Spec decision 1 (``docs/superpowers/specs/2026-11-25-adrv9361z7035-hop-design.md``): Tick
``gpio_out[2:0]`` drives CTRL_IN[2:0]; with Profile Pin Select set in both 0x25A (Rx Fast Lock
Setup) and 0x29A (Tx Fast Lock Setup), one 3-bit code selects RX profile k and TX profile k
together. Sequence (vrt49 ``scripts/adrv9361_tick_hop.py``, extended to TX):

1. pin control off in the driver (``adi,{rx,tx}-fastlock-pincontrol-enable`` debug attributes),
   so ``fastlock_recall`` writes SPI profile select only;
2. for each slot k: RX LO then ``fastlock_store k`` on ``altvoltage0`` (RX_LO), TX LO then
   ``fastlock_store k`` on ``altvoltage1`` (TX_LO);
3. ``fastlock_recall 0`` on both; check 0x25A/0x29A read Fast Lock Mode Enable, profile 0, pin
   select off;
4. (Tick parks CTRL_IN at code 0 — :meth:`hopcap.scheduler.TickSession.park_trigger_low`);
5. set Profile Pin Select by read-modify-write of D1|D0 in 0x25A and 0x29A through the PHY's
   debugfs ``direct_reg_access`` on the board, and read both back.

After step 5 nothing may write an LO or recall a profile: the driver rewrites the setup
registers on either. The driver's cached LO (``frequency`` readback) is what the host last
wrote, not what a pin recall selected, so it is never used as evidence of a hop:
:meth:`readback_hz` returns ``None``.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from hopcap.plan import FastLockSlot

REG_RX_FASTLOCK_SETUP = 0x25A
REG_TX_FASTLOCK_SETUP = 0x29A
SETUP_REGS = (REG_RX_FASTLOCK_SETUP, REG_TX_FASTLOCK_SETUP)
FASTLOCK_MODE_ENABLE = 1 << 0  # D0
PROFILE_PIN_SELECT = 1 << 1  # D1
PROFILE_SHIFT, PROFILE_MASK = 5, 0x7 << 5  # D7:D5, SPI-selected profile
RX_LO, TX_LO = "altvoltage0", "altvoltage1"
PINCONTROL_ATTRS = ("adi,rx-fastlock-pincontrol-enable", "adi,tx-fastlock-pincontrol-enable")

ChannelWrite = Callable[[str, str, str], None]  # (channel, attribute, value)
DebugWrite = Callable[[str, str], None]  # (debug attribute, value)


class FastLockError(RuntimeError):
    pass


_REG_LINE = re.compile(r"^REG (0x[0-9A-Fa-f]+) (0x[0-9A-Fa-f]+|\d+)(?: (0x[0-9A-Fa-f]+|\d+))?$")


class PhyRegs:
    """AD9361 SPI registers through the PHY's debugfs ``direct_reg_access`` over a board shell
    (``hopcap.transport.Shell``). One shell call per operation; the read-modify-write runs on the
    board so nothing else can interleave between its read and its write over the network."""

    def __init__(self, shell, *, device: str = "ad9361-phy"):
        self.shell = shell
        self.device = device

    def _prologue(self) -> str:
        dev = shlex.quote(self.device)
        return (
            "set -e; "
            "[ -d /sys/kernel/debug/iio ] || mount -t debugfs none /sys/kernel/debug; "
            "d=''; for n in /sys/bus/iio/devices/iio:device*; do "
            f'[ "$(cat $n/name)" = {dev} ] && d=$(basename $n); done; '
            f'[ -n "$d" ] || {{ echo "no IIO device {self.device}" >&2; exit 3; }}; '
            "f=/sys/kernel/debug/iio/$d/direct_reg_access; "
            '[ -w "$f" ] || { echo "no $f" >&2; exit 3; }; '
        )

    def _run(self, body: str) -> dict[int, tuple[int, int | None]]:
        out = self.shell.run(self._prologue() + body)
        regs: dict[int, tuple[int, int | None]] = {}
        for line in out.splitlines():
            m = _REG_LINE.match(line.strip())
            if m:
                after = int(m.group(3), 0) if m.group(3) is not None else None
                regs[int(m.group(1), 16)] = (int(m.group(2), 0), after)
        return regs

    def read(self, reg: int) -> int:
        got = self._run(f'echo 0x{reg:X} > "$f"; echo "REG 0x{reg:X} $(cat "$f")"')
        if reg not in got:
            raise FastLockError(f"no readback for register 0x{reg:X}")
        return got[reg][0]

    def update(self, regs: Sequence[int], *, set_bits: int, clear_bits: int = 0) -> dict:
        """Read-modify-write every register on the board; returns {reg: (before, after)} with
        ``after`` read back from the chip after the write."""
        parts = []
        for reg in regs:
            parts.append(
                f'echo 0x{reg:X} > "$f"; v=$(cat "$f"); '
                f"n=$(( (v & ~{clear_bits}) | {set_bits} )); "
                f'echo "0x{reg:X} $n" > "$f"; echo 0x{reg:X} > "$f"; '
                f'echo "REG 0x{reg:X} $v $(cat "$f")"'
            )
        got = self._run("; ".join(parts))
        missing = [r for r in regs if r not in got or got[r][1] is None]
        if missing:
            raise FastLockError(f"no read-modify-write record for {[hex(r) for r in missing]}")
        return {r: (got[r][0], got[r][1]) for r in regs}


def decode_setup(value: int) -> dict:
    return {
        "raw": value,
        "mode_enable": bool(value & FASTLOCK_MODE_ENABLE),
        "pin_select": bool(value & PROFILE_PIN_SELECT),
        "profile": (value & PROFILE_MASK) >> PROFILE_SHIFT,
    }


@dataclass
class FastLockBank:
    """Program the RX and TX FastLock profiles and hand profile selection to CTRL_IN.

    Also the radio adapter :func:`hopcap.run.run_live` drives (``prepare`` / ``enable`` /
    ``disable`` / ``stage`` / ``readback_hz``), in the order that run_live already keeps: prepare
    (store + recall 0), park CTRL_IN at 0 through Tick, enable (pin select), hops, disable."""

    write_channel: ChannelWrite
    write_debug: DebugWrite
    regs: PhyRegs
    log: Callable[[str], None] = print
    slots: tuple[FastLockSlot, ...] = ()
    loaded: bool = False
    enabled: bool = False
    record: dict = field(default_factory=dict)

    # -- bank ---------------------------------------------------------------------------
    def load(self, slots: Sequence[FastLockSlot]) -> dict:
        slots = tuple(slots)
        if not 1 <= len(slots) <= 8:
            raise FastLockError(f"{len(slots)} slots: the FastLock bank holds 1..8 profiles")
        if self.enabled:
            raise FastLockError("pin select is on: disable it before writing LOs")
        for attr in PINCONTROL_ATTRS:
            self.write_debug(attr, "0")
        for k, s in enumerate(slots):
            self.write_channel(RX_LO, "frequency", str(int(s.rx_lo_hz)))
            self.write_channel(RX_LO, "fastlock_store", str(k))
            self.write_channel(TX_LO, "frequency", str(int(s.tx_lo_hz)))
            self.write_channel(TX_LO, "fastlock_store", str(k))
        self.write_channel(RX_LO, "fastlock_recall", "0")
        self.write_channel(TX_LO, "fastlock_recall", "0")
        after = {reg: self.regs.read(reg) for reg in SETUP_REGS}
        for reg, v in after.items():
            d = decode_setup(v)
            if not d["mode_enable"] or d["pin_select"] or d["profile"] != 0:
                raise FastLockError(
                    f"after recall 0, register 0x{reg:X} = 0x{v:02X} ({d}): expected Fast Lock "
                    "Mode Enable, SPI profile 0, pin select off"
                )
        self.slots, self.loaded = slots, True
        self.record["loaded"] = {
            "slots": [{"rx_lo_hz": s.rx_lo_hz, "tx_lo_hz": s.tx_lo_hz} for s in slots],
            "setup_after_recall": {f"0x{r:X}": v for r, v in after.items()},
        }
        self.log(f"fastlock: {len(slots)} RX+TX profiles stored, profile 0 recalled")
        return self.record["loaded"]

    def enable_pin_select(self) -> dict:
        """D1|D0 in 0x25A and 0x29A: CTRL_IN[2:0] selects RX and TX profile together. Call only
        after Tick has parked CTRL_IN at code 0 (profile 0, the one just recalled)."""
        if not self.loaded:
            raise FastLockError("FastLock bank not loaded: store and recall before pin select")
        bits = PROFILE_PIN_SELECT | FASTLOCK_MODE_ENABLE
        res = self.regs.update(SETUP_REGS, set_bits=bits)
        for reg, (before, after) in res.items():
            if after & bits != bits or (after & ~bits) != (before & ~bits):
                raise FastLockError(
                    f"pin select on 0x{reg:X}: 0x{before:02X} -> 0x{after:02X} (expected D1|D0 set, "
                    "other bits kept)"
                )
        self.enabled = True
        self.record["pin_select_on"] = {f"0x{r:X}": list(v) for r, v in res.items()}
        self.log("fastlock: profile pin select on (0x25A, 0x29A D1|D0)")
        return self.record["pin_select_on"]

    def disable_pin_select(self) -> dict:
        res = self.regs.update(SETUP_REGS, set_bits=0, clear_bits=PROFILE_PIN_SELECT)
        bad = {r: v for r, v in res.items() if v[1] & PROFILE_PIN_SELECT}
        self.enabled = False
        self.record["pin_select_off"] = {f"0x{r:X}": list(v) for r, v in res.items()}
        if bad:
            raise FastLockError(f"pin select still set after clearing: {bad}")
        return self.record["pin_select_off"]

    def snapshot(self) -> dict:
        return {f"0x{r:X}": decode_setup(self.regs.read(r)) for r in SETUP_REGS}

    # -- the radio adapter run_live drives ---------------------------------------------------
    def prepare(self, plan) -> None:
        """Store the plan's slots and recall profile 0 (before Tick parks CTRL_IN at 0)."""
        slots = getattr(plan, "slots", None)
        if not slots or not isinstance(slots[0], FastLockSlot):
            raise FastLockError("FastLockBank needs a FastLock (ADRV9361) plan")
        self.load(slots)

    def enable(self) -> None:
        self.enable_pin_select()

    def disable(self) -> None:
        if self.enabled:
            self.disable_pin_select()

    def stage(self, hz: int) -> None:
        raise FastLockError("FastLock hops are pin-selected; nothing is staged")

    def readback_hz(self) -> None:
        """No evidence available host-side: the driver's cached LO is not a pin recall."""
        return None
