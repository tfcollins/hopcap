# SPDX-License-Identifier: MIT
"""Edges -> Tick GPIO_WRITE descriptors, checked against the Tick model, not by eye."""

from __future__ import annotations

import struct

from tick import CommandDescriptor, CommandID, Flag, TargetFamily
from tick.ffh import descriptor_csv
from tick.hdl_model import AxiTimedCommandSchedulerModel

from hopcap.descriptors import (
    FALL_SEQ_BASE,
    RISE_SEQ_BASE,
    descriptors_for_edge,
    descriptors_for_plan,
    gpio_value,
)
from hopcap.plan import HopPlan, compile_plan

TICK_HZ = 100_000_000
MHZ = 1_000_000


def compiled():
    p = HopPlan(
        frequencies_hz=(2_410 * MHZ, 2_420 * MHZ),
        period_ticks=50_000,
        settle_ticks=2_000,
        capture_ticks=10_000,
        pulse_ticks=1_000,
        lead_ticks=10_000,
        context_margin_ticks=100,
    )
    return compile_plan(p, t0_ticks=0, tick_hz=TICK_HZ)


def test_gpio_value_puts_slot_on_bits_3_1_and_trigger_on_bit_0():
    assert gpio_value(slot=5, trigger=True) == 0b1011
    assert gpio_value(slot=5, trigger=False) == 0b1010


def test_edge_yields_rise_with_completion_and_fall_without():
    c = compiled()
    rise, fall = descriptors_for_edge(c.edges[1])
    assert rise.command_id == CommandID.GPIO_WRITE and rise.target_id == TargetFamily.GPIO
    assert rise.timestamp_ticks == c.edges[1].rise_ticks
    assert rise.args == (0xF, gpio_value(c.edges[1].slot, True), 0, 0)
    assert rise.flags == Flag.REPORT_COMPLETION | Flag.LATE_DROP
    assert rise.sequence_id == RISE_SEQ_BASE + 1
    assert fall.timestamp_ticks == c.edges[1].fall_ticks
    assert fall.args == (0x1, 0, 0, 0), "falling edge touches only the trigger bit"
    assert fall.flags == Flag.LATE_DROP
    assert fall.sequence_id == FALL_SEQ_BASE + 1


def test_pack_matches_the_48_byte_wire_contract():
    c = compiled()
    rise, _ = descriptors_for_edge(c.edges[0])
    expected = struct.pack(
        "<HHHHQII4I8x",
        1,
        48,
        CommandID.GPIO_WRITE,
        TargetFamily.GPIO,
        rise.timestamp_ticks,
        int(rise.flags),
        rise.sequence_id,
        0xF,
        gpio_value(c.edges[0].slot, True),
        0,
        0,
    )
    assert rise.pack() == expected
    assert CommandDescriptor.unpack(rise.pack()) == rise
    assert len(descriptor_csv(rise).split(",")) == 12


def test_replay_through_tick_model_drives_the_expected_gpio_waveform():
    c = compiled()
    model = AxiTimedCommandSchedulerModel(queue_depth=16, tick_hz=TICK_HZ)
    for d in descriptors_for_plan(c):
        model.push_gpio(
            d.sequence_id,
            d.timestamp_ticks,
            d.args[0],
            d.args[1],
            late_policy="drop",
            report_completion=bool(d.flags & Flag.REPORT_COMPLETION),
        )
    seen_rises = []
    for e in c.edges:
        model.advance_to(e.rise_ticks - 1)
        assert model.gpio_out & 1 == 0, f"trigger high before edge {e.index}"
        model.advance_to(e.rise_ticks)
        assert model.gpio_out & 1 == 1, f"trigger did not rise at edge {e.index}"
        assert (model.gpio_out >> 1) & 0x7 == e.slot, f"slot bits wrong at edge {e.index}"
        seen_rises.append(model.gpio_out)
        # One-entry completion window: the host must ack each rise before the next
        # reporting command may execute.  Mirror that, and check the identity.
        assert model.completion is not None and model.completion_pending
        assert model.completion.sequence_id == RISE_SEQ_BASE + e.index
        assert model.completion.executed_timestamp_ticks == e.rise_ticks
        model.ack_completion()
        model.advance_to(e.fall_ticks)
        assert model.gpio_out & 1 == 0, f"trigger did not fall after edge {e.index}"
        assert (model.gpio_out >> 1) & 0x7 == e.slot, "falling edge must not disturb slot bits"
    assert len(seen_rises) == len(c.edges)
    assert model.late_count == 0 and model.overflow_count == 0
