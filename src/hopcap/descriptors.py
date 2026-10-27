# SPDX-License-Identifier: MIT
"""Compiled edges -> Tick ``GPIO_WRITE`` descriptors.

Bit layout on Tick ``gpio_out``: bit 0 is the FHM trigger (wired to TAL_GPIO_0 and to
the hop controller's event input), bits 3:1 carry the slot index for the hop controller.
Each edge is two descriptors: a rising edge that writes trigger+slot and reports a
completion, and a falling edge ``pulse_ticks`` later that clears only bit 0 and reports
nothing (so the one-entry completion window never head-of-line blocks on it).
"""

from __future__ import annotations

from collections.abc import Iterable

from tick import CommandDescriptor, CommandID, Flag, TargetFamily

from hopcap.plan import CompiledPlan, Edge

TRIGGER_MASK = 0x1
SLOT_SHIFT = 1
SLOT_MASK = 0x7 << SLOT_SHIFT
GPIO_MASK = TRIGGER_MASK | SLOT_MASK  # 0xF

RISE_SEQ_BASE = 0x1000
FALL_SEQ_BASE = 0x2000


def gpio_value(slot: int, trigger: bool) -> int:
    if not 0 <= slot <= 7:
        raise ValueError(f"slot {slot} out of range 0..7")
    return (slot << SLOT_SHIFT) | (TRIGGER_MASK if trigger else 0)


def descriptors_for_edge(edge: Edge) -> tuple[CommandDescriptor, CommandDescriptor]:
    rise = CommandDescriptor(
        command_id=CommandID.GPIO_WRITE,
        target_id=TargetFamily.GPIO,
        timestamp_ticks=edge.rise_ticks,
        flags=Flag.REPORT_COMPLETION | Flag.LATE_DROP,
        sequence_id=RISE_SEQ_BASE + edge.index,
        args=(GPIO_MASK, gpio_value(edge.slot, True), 0, 0),
    )
    fall = CommandDescriptor(
        command_id=CommandID.GPIO_WRITE,
        target_id=TargetFamily.GPIO,
        timestamp_ticks=edge.fall_ticks,
        flags=Flag.LATE_DROP,
        sequence_id=FALL_SEQ_BASE + edge.index,
        args=(TRIGGER_MASK, 0, 0, 0),
    )
    return rise, fall


def descriptors_for_plan(compiled: CompiledPlan) -> list[CommandDescriptor]:
    out: list[CommandDescriptor] = []
    for edge in compiled.edges:
        out.extend(descriptors_for_edge(edge))
    return out


def descriptors_for_edges(edges: Iterable[Edge]) -> list[CommandDescriptor]:
    out: list[CommandDescriptor] = []
    for edge in edges:
        out.extend(descriptors_for_edge(edge))
    return out
