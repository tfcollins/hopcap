# SPDX-License-Identifier: MIT
"""Programmer for the ``vrt49_fhm_hop_ctrl`` block.

Register offsets come from ``hdl/hopctrl_regmap.json`` at import time so this module can
never disagree with the RTL (both are tested against the same file).  The transport is any
object with ``read32(offset) -> int`` and ``write32(offset, value)``; see
:mod:`hopcap.transport` for the on-target ``/dev/mem`` helper and the ``devmem`` fallback.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hopcap.plan import HopPlan, SlotTable

REGMAP_PATH = Path(__file__).resolve().parents[2] / "hdl" / "hopctrl_regmap.json"

CTRL_ENABLE = 1 << 0
CTRL_SLOT_SOURCE_SEQ = 1 << 1
CTRL_W1C_CLEAR = 1 << 2
CTRL_IDLE_OPEN = 1 << 3

ST_STATE_MASK = 0x7
ST_GATE_OPEN = 1 << 3
ST_SUPERSEDED = 1 << 4
ST_DRAIN_TIMEOUT = 1 << 5
ST_ENABLE = 1 << 8
ST_SLOT_SOURCE = 1 << 9
ST_IDLE_OPEN = 1 << 11


class HopCtrlError(RuntimeError):
    pass


class Mmio(Protocol):
    def read32(self, offset: int) -> int: ...
    def write32(self, offset: int, value: int) -> None: ...


class HopCtrlRegs:
    """Attribute access to every register offset in the JSON map (``regs.SETTLE_TICKS``)."""

    def __init__(self, data: dict):
        self._data = data
        self.id_word = int(data["id_word"], 16)
        self.id_ascii = data["id_ascii"]
        self.version_word = int(data["version_word"], 16)
        self.slot_count = int(data["slot_count"])
        self.states: dict[str, int] = dict(data["states"])
        self.state_names = {v: k for k, v in self.states.items()}
        self.offsets: dict[str, int] = {r["name"]: int(r["offset"], 16) for r in data["registers"]}
        for name, off in self.offsets.items():
            setattr(self, name, off)

    @classmethod
    def from_json(cls, path: Path | str = REGMAP_PATH) -> HopCtrlRegs:
        return cls(json.loads(Path(path).read_text()))

    def slot_lo(self, i: int) -> int:
        return self.offsets["SLOT0_LO"] + 8 * i

    def slot_hi(self, i: int) -> int:
        return self.offsets["SLOT0_HI"] + 8 * i


@dataclass(frozen=True)
class Status:
    raw: int
    state: int
    state_name: str
    gate_open: bool
    superseded_sticky: bool
    drain_timeout_sticky: bool
    enabled: bool
    slot_source_seq: bool
    idle_open: bool
    epoch: int
    event_count: int
    capture_count: int
    superseded_count: int
    last_slot: int
    gpio_seen: int

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class HopController:
    def __init__(self, mmio: Mmio, regs: HopCtrlRegs | None = None):
        self.m = mmio
        self.r = regs or HopCtrlRegs.from_json()

    # -- identity -----------------------------------------------------------
    def identify(self) -> tuple[str, int]:
        word = self.m.read32(self.r.ID)
        ascii_ = word.to_bytes(4, "big").decode("ascii", errors="replace")
        if word != self.r.id_word:
            raise HopCtrlError(
                f"hop controller ID 0x{word:08X} ({ascii_!r}) != expected "
                f"0x{self.r.id_word:08X} ({self.r.id_ascii!r})"
            )
        return ascii_, self.m.read32(self.r.VERSION)

    # -- programming ----------------------------------------------------------
    def program(self, plan: HopPlan, slots: SlotTable, *, sequence_mode: bool = False,
                drain_timeout_ticks: int = 0) -> None:
        """Write slot table, settle, capture, then enable with stickies cleared."""
        self.m.write32(self.r.CTRL_STATUS, CTRL_IDLE_OPEN)  # disable while reprogramming
        for i, word in enumerate(slots.words()):
            self.m.write32(self.r.slot_lo(i), word & 0xFFFFFFFF)
            self.m.write32(self.r.slot_hi(i), (word >> 32) & 0xFFFFFFFF)
        self.m.write32(self.r.SETTLE_TICKS, plan.settle_ticks)
        self.m.write32(self.r.CAPTURE_TICKS, plan.capture_ticks)
        self.m.write32(self.r.DRAIN_TIMEOUT_TICKS, drain_timeout_ticks)
        self.m.write32(self.r.SEQ_SLOT_COUNT, max(1, len(slots)))
        self.m.write32(self.r.SEQ_INDEX, 0)
        ctrl = CTRL_ENABLE | CTRL_IDLE_OPEN | CTRL_W1C_CLEAR
        if sequence_mode:
            ctrl |= CTRL_SLOT_SOURCE_SEQ
        self.m.write32(self.r.CTRL_STATUS, ctrl)

    def disable(self) -> None:
        """Leave the gate forced open (gapless, pre-hopcap behaviour)."""
        self.m.write32(self.r.CTRL_STATUS, CTRL_IDLE_OPEN)

    def clear_stickies(self) -> None:
        st = self.m.read32(self.r.CTRL_STATUS)
        keep = (CTRL_ENABLE if st & ST_ENABLE else 0) | (CTRL_SLOT_SOURCE_SEQ if st & ST_SLOT_SOURCE else 0) \
            | (CTRL_IDLE_OPEN if st & ST_IDLE_OPEN else 0)
        self.m.write32(self.r.CTRL_STATUS, keep | CTRL_W1C_CLEAR)

    # -- observation ------------------------------------------------------------
    def status(self) -> Status:
        raw = self.m.read32(self.r.CTRL_STATUS)
        state = raw & ST_STATE_MASK
        return Status(
            raw=raw,
            state=state,
            state_name=self.r.state_names.get(state, f"?{state}"),
            gate_open=bool(raw & ST_GATE_OPEN),
            superseded_sticky=bool(raw & ST_SUPERSEDED),
            drain_timeout_sticky=bool(raw & ST_DRAIN_TIMEOUT),
            enabled=bool(raw & ST_ENABLE),
            slot_source_seq=bool(raw & ST_SLOT_SOURCE),
            idle_open=bool(raw & ST_IDLE_OPEN),
            epoch=self.m.read32(self.r.EPOCH),
            event_count=self.m.read32(self.r.EVENT_COUNT),
            capture_count=self.m.read32(self.r.CAPTURE_COUNT),
            superseded_count=self.m.read32(self.r.SUPERSEDED_COUNT),
            last_slot=self.m.read32(self.r.LAST_SLOT) & 0x7,
            gpio_seen=self.m.read32(self.r.GPIO_SEEN) & 0xF,
        )

    def require_idle(self) -> Status:
        """Fail closed unless the controller is enabled, in IDLE, and carries no stickies."""
        st = self.status()
        if not st.enabled:
            raise HopCtrlError("hop controller is not enabled")
        if st.state != self.r.states["IDLE"]:
            raise HopCtrlError(f"hop controller state is {st.state_name}, expected IDLE")
        if st.drain_timeout_sticky:
            raise HopCtrlError("drain-timeout sticky is set")
        if st.superseded_sticky or st.superseded_count:
            raise HopCtrlError(f"superseded sticky set (count {st.superseded_count})")
        return st

    def wait_drained(self, *, expected_events: int, timeout_s: float = 2.0,
                     poll_s: float = 0.02, sleep: Callable[[float], None] = time.sleep) -> Status:
        """After the last edge: wait until no Context is in flight and the gate has shut
        or returned to idle, then check the epoch advanced by exactly ``expected_events``."""
        quiet = {self.r.states["IDLE"], self.r.states["OPEN"], self.r.states["CLOSED"]}
        deadline = time.monotonic() + timeout_s
        while True:
            st = self.status()
            if st.state in quiet:
                break
            if time.monotonic() >= deadline:
                raise HopCtrlError(f"hop controller still in {st.state_name} after {timeout_s}s")
            sleep(poll_s)
        if st.epoch != expected_events:
            raise HopCtrlError(f"epoch {st.epoch} != expected events {expected_events}")
        if st.drain_timeout_sticky:
            raise HopCtrlError("drain-timeout sticky set during the run")
        return st
