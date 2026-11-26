# SPDX-License-Identifier: MIT
"""tap_accept against the real vrt49_sample_tap: one pulse per accepted beat, none per drop.

The downstream ready is random, so the tap drops beats; every input beat must be either
accepted (a pulse one clock after the edge that sampled it) or dropped, never both and never neither, and the
pulse count must equal the beats the tap forwarded (plus one still held in its register).
"""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge


@cocotb.test()
async def accepted_counts_exactly_the_beats_that_were_not_dropped(dut):
    rng = random.Random(7)
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.resetn.value = 0
    dut.sample_valid.value = 0
    dut.sample_data.value = 0
    dut.m_axis_tready.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.resetn.value = 1

    inputs: list[bool] = []  # sample_valid driven for each rising edge
    pulses: list[bool] = []  # accepted after each rising edge
    drops: list[int] = []
    for i in range(4000):
        v = rng.random() < 0.6
        dut.sample_valid.value = int(v)
        dut.sample_data.value = i
        dut.m_axis_tready.value = int(rng.random() < 0.45)  # often not ready: drops
        await RisingEdge(dut.clk)
        await ReadOnly()
        inputs.append(v)
        pulses.append(bool(int(dut.accepted.value)))
        drops.append(int(dut.dropped_sample_count.value))
        await FallingEdge(dut.clk)
    dut.sample_valid.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
        await ReadOnly()
        inputs.append(False)
        pulses.append(bool(int(dut.accepted.value)))
        drops.append(int(dut.dropped_sample_count.value))
        await FallingEdge(dut.clk)

    total = sum(inputs)
    dropped = drops[-1]
    accepted = sum(pulses)
    assert dropped > total // 10, "the test must exercise drops"
    assert accepted + dropped == total
    held = int(dut.m_axis_tvalid.value)
    assert accepted == int(dut.forwarded_sample_count.value) + held
    # the tap samples beat i at edge i and counts a drop on that same edge; the pulse
    # follows one edge later
    for i, v in enumerate(inputs[:-3]):
        was_dropped = drops[i] != (drops[i - 1] if i else 0)
        assert pulses[i + 1] == (v and not was_dropped), f"edge {i}"
