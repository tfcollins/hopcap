# SPDX-License-Identifier: MIT
"""Compiled edges -> Tick ``GPIO_WRITE`` descriptors.

The bit layout on Tick ``gpio_out`` is the platform's :class:`hopcap.platform.GpioBitMap`:

* ZU11EG (default): bit 0 is the FHM trigger (wired to TAL_GPIO_0 and to the hop controller's
  event input), bits 3:1 carry the slot index for the hop controller.
* ADRV9361: bits 2:0 are AD9361 CTRL_IN[2:0] (the FastLock profile, which is also the slot) and
  bit 3 is the event: rise ``mask 0xF value 0x8|slot``, fall ``mask 0x8 value 0``.

Each edge is two descriptors: a rising edge that writes event+slot and reports a completion,
and a falling edge ``pulse_ticks`` later that clears only the event bit and reports nothing (so
the one-entry completion window never head-of-line blocks on it).
"""

from __future__ import annotations

from collections.abc import Iterable

from tick import CommandDescriptor, CommandID, Flag, TargetFamily

from hopcap.plan import CompiledPlan, Edge
from hopcap.platform import ZU11EG_BITS, GpioBitMap

TRIGGER_MASK = ZU11EG_BITS.event_mask
SLOT_SHIFT = ZU11EG_BITS.slot_shift
SLOT_MASK = ZU11EG_BITS.slot_mask
GPIO_MASK = TRIGGER_MASK | SLOT_MASK  # 0xF

RISE_SEQ_BASE = 0x1000
FALL_SEQ_BASE = 0x2000


def gpio_value(slot: int, trigger: bool, bits: GpioBitMap = ZU11EG_BITS) -> int:
    return bits.value(slot, trigger)


def descriptors_for_edge(
    edge: Edge, bits: GpioBitMap = ZU11EG_BITS
) -> tuple[CommandDescriptor, CommandDescriptor]:
    rise_mask, rise_value = bits.rise(edge.slot)
    fall_mask, fall_value = bits.fall()
    rise = CommandDescriptor(
        command_id=CommandID.GPIO_WRITE,
        target_id=TargetFamily.GPIO,
        timestamp_ticks=edge.rise_ticks,
        flags=Flag.REPORT_COMPLETION | Flag.LATE_DROP,
        sequence_id=RISE_SEQ_BASE + edge.index,
        args=(rise_mask, rise_value, 0, 0),
    )
    fall = CommandDescriptor(
        command_id=CommandID.GPIO_WRITE,
        target_id=TargetFamily.GPIO,
        timestamp_ticks=edge.fall_ticks,
        flags=Flag.LATE_DROP,
        sequence_id=FALL_SEQ_BASE + edge.index,
        args=(fall_mask, fall_value, 0, 0),
    )
    return rise, fall


def descriptors_for_plan(
    compiled: CompiledPlan, bits: GpioBitMap = ZU11EG_BITS
) -> list[CommandDescriptor]:
    return descriptors_for_edges(compiled.edges, bits)


def descriptors_for_edges(
    edges: Iterable[Edge], bits: GpioBitMap = ZU11EG_BITS
) -> list[CommandDescriptor]:
    out: list[CommandDescriptor] = []
    for edge in edges:
        out.extend(descriptors_for_edge(edge, bits))
    return out
