# SPDX-License-Identifier: MIT
"""Unit tests for tick_pps_gen: the internal 1PPS on the Tick clock (adrv9361 profile).

Built with small parameters (Makefile TOP=tick_pps_gen passes them with -G and exports them as
PPS_GEN_*): a "second" is TICK_HZ cycles.  resetn is driven on the falling edge and pps_out is
read after every rising edge, so each test checks exact per-cycle traces:

  * held low through reset;
  * with resetn released before rising edge 0, pps_out is high after edges 0..PULSE_TICKS-1 of
    every TICK_HZ-cycle second and low otherwise (exact period and width, no drift);
  * a reset in the middle of a pulse drops it on the next edge and restarts the phase.
"""

from __future__ import annotations

import os
from itertools import pairwise

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

TICK_HZ = int(os.environ.get("PPS_GEN_TICK_HZ", "20"))
PULSE_TICKS = int(os.environ.get("PPS_GEN_PULSE_TICKS", "3"))


async def _start(dut, reset_cycles: int = 4) -> None:
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.resetn.value = 0
    for _ in range(reset_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.pps_out.value) == 0, "pps_out must stay low in reset"
    await FallingEdge(dut.clk)
    dut.resetn.value = 1


async def _trace(dut, n: int) -> list[int]:
    out = []
    for _ in range(n):
        await RisingEdge(dut.clk)
        await ReadOnly()
        out.append(int(dut.pps_out.value))
    return out


def _expected(n: int, start_phase: int = 0) -> list[int]:
    return [1 if (k + start_phase) % TICK_HZ < PULSE_TICKS else 0 for k in range(n)]


def _rises(trace: list[int]) -> list[int]:
    return [i for i, (a, b) in enumerate(pairwise([0, *trace])) if b and not a]


@cocotb.test()
async def test_exact_period_and_width(dut):
    await _start(dut)
    n = 5 * TICK_HZ + 7
    trace = await _trace(dut, n)
    assert trace == _expected(n), f"trace {trace}"
    rises = _rises(trace)
    # first edge one register delay after reset release, then exactly TICK_HZ apart
    assert rises[0] == 0, rises
    assert all(b - a == TICK_HZ for a, b in pairwise(rises)), rises
    assert len(rises) == 6
    # width exactly PULSE_TICKS cycles
    for r in rises:
        assert trace[r : r + PULSE_TICKS] == [1] * PULSE_TICKS
        assert trace[r + PULSE_TICKS] == 0


@cocotb.test()
async def test_reset_mid_pulse_restarts_phase(dut):
    await _start(dut)
    first = await _trace(dut, TICK_HZ + 1)  # into the second pulse (cycle TICK_HZ is high)
    assert first[-1] == 1
    await FallingEdge(dut.clk)
    dut.resetn.value = 0
    held = await _trace(dut, 3)
    assert held == [0, 0, 0], "reset must drop the pulse on the next edge"
    await FallingEdge(dut.clk)
    dut.resetn.value = 1
    n = 2 * TICK_HZ + 1
    trace = await _trace(dut, n)
    assert trace == _expected(n), f"trace after reset {trace}"
