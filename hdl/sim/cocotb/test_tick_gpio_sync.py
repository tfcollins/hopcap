# SPDX-License-Identifier: MIT
"""tick_gpio_sync: the slot bits settle at least one cycle before the event rises."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer


async def _cycles(dut, n):
    for _ in range(n):
        await RisingEdge(dut.clk)


@cocotb.test()
async def slot_valid_when_event_rises(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.gpio_async.value = 0
    await _cycles(dut, 5)
    for slot in (5, 2, 7, 0, 3):
        # Event and slot change together, as Tick writes them in one command.
        await Timer(3, units="ns")
        dut.gpio_async.value = (slot << 1) | 1
        seen = None
        for cyc in range(6):
            await RisingEdge(dut.clk)
            await Timer(1, units="ns")
            out = int(dut.gpio_sync.value)
            if out & 1:
                seen = cyc
                assert out >> 1 == slot, f"event rose with slot {out >> 1}, expected {slot}"
                # The slot was already there one cycle earlier.
                break
            last_slot = out >> 1
        assert seen is not None, "event never rose"
        assert last_slot == slot, f"slot {last_slot} not settled the cycle before the event"
        dut.gpio_async.value = slot << 1
        await _cycles(dut, 5)
        assert int(dut.gpio_sync.value) & 1 == 0
