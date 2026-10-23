# SPDX-License-Identifier: MIT
"""Unit tests for vrt49_fhm_hop_ctrl: register file, event detection, gate state machine."""
from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

from hopctrl_regs import ID_WORD, REG, STATE, VERSION_WORD, slot_hi, slot_lo

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


async def axi_write(dut, address: int, value: int, strb: int = 0xF) -> None:
    dut.s_axi_awaddr.value = address
    dut.s_axi_awvalid.value = 1
    dut.s_axi_wdata.value = value
    dut.s_axi_wstrb.value = strb
    dut.s_axi_wvalid.value = 1
    aw_done = w_done = False
    while not (aw_done and w_done):
        await RisingEdge(dut.clk)
        if not aw_done and dut.s_axi_awready.value:
            dut.s_axi_awvalid.value = 0
            aw_done = True
        if not w_done and dut.s_axi_wready.value:
            dut.s_axi_wvalid.value = 0
            w_done = True
    dut.s_axi_bready.value = 1
    while not dut.s_axi_bvalid.value:
        await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.s_axi_bready.value = 0


async def axi_read(dut, address: int) -> int:
    dut.s_axi_araddr.value = address
    dut.s_axi_arvalid.value = 1
    while True:
        await RisingEdge(dut.clk)
        if dut.s_axi_arready.value:
            dut.s_axi_arvalid.value = 0
            break
    dut.s_axi_rready.value = 1
    while not dut.s_axi_rvalid.value:
        await RisingEdge(dut.clk)
    value = int(dut.s_axi_rdata.value)
    await RisingEdge(dut.clk)
    dut.s_axi_rready.value = 0
    return value


async def reset(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.resetn.value = 0
    dut.scheduler_gpio.value = 0
    dut.fabric_samples_drained.value = 0
    dut.fabric_context_ready.value = 0
    dut.fabric_context_accepted.value = 0
    for sig in ("s_axi_awvalid", "s_axi_wvalid", "s_axi_bready", "s_axi_arvalid", "s_axi_rready"):
        getattr(dut, sig).value = 0
    dut.s_axi_awprot.value = 0
    dut.s_axi_arprot.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.resetn.value = 1
    for _ in range(2):
        await RisingEdge(dut.clk)


async def status(dut) -> int:
    return await axi_read(dut, REG["CTRL_STATUS"])


async def state_of(dut) -> int:
    return (await status(dut)) & ST_STATE_MASK


@cocotb.test()
async def id_and_version_read_back(dut):
    await reset(dut)
    assert await axi_read(dut, REG["ID"]) == ID_WORD
    assert await axi_read(dut, REG["VERSION"]) == VERSION_WORD


@cocotb.test()
async def reset_defaults_match_regmap(dut):
    await reset(dut)
    st = await status(dut)
    assert st & ST_ENABLE == 0, "controller must come up disabled"
    assert st & ST_IDLE_OPEN, "idle_open defaults to 1"
    assert st & ST_SLOT_SOURCE == 0
    assert st & ST_STATE_MASK == STATE["IDLE"]
    assert st & ST_GATE_OPEN, "disabled controller forces the gate open"
    assert await axi_read(dut, REG["SETTLE_TICKS"]) == 0x7D0
    assert await axi_read(dut, REG["CAPTURE_TICKS"]) == 0
    assert await axi_read(dut, REG["SEQ_SLOT_COUNT"]) == 8
    assert await axi_read(dut, REG["SEQ_INDEX"]) == 0
    assert await axi_read(dut, REG["DRAIN_TIMEOUT_TICKS"]) == 0
    assert await axi_read(dut, REG["EPOCH"]) == 0
    assert await axi_read(dut, REG["EVENT_COUNT"]) == 0
    for i in range(8):
        assert await axi_read(dut, slot_lo(i)) == 0
        assert await axi_read(dut, slot_hi(i)) == 0


@cocotb.test()
async def slot_table_honours_byte_strobes(dut):
    await reset(dut)
    await axi_write(dut, slot_lo(3), 0x11223344)
    await axi_write(dut, slot_hi(3), 0x55667788)
    await axi_write(dut, slot_lo(3), 0xAABBCCDD, strb=0b0010)  # only byte lane 1
    assert await axi_read(dut, slot_lo(3)) == 0x1122CC44
    assert await axi_read(dut, slot_hi(3)) == 0x55667788
    assert await axi_read(dut, slot_lo(2)) == 0, "neighbouring slot untouched"


@cocotb.test()
async def disabled_controller_ignores_edges_and_keeps_gate_open(dut):
    await reset(dut)
    dut.scheduler_gpio.value = 0b0011  # rising edge on bit 0, slot 1
    for _ in range(4):
        await RisingEdge(dut.clk)
    assert await axi_read(dut, REG["EVENT_COUNT"]) == 0
    assert await axi_read(dut, REG["EPOCH"]) == 0
    assert await state_of(dut) == STATE["IDLE"]
    await ReadOnly()
    assert dut.stream_enable.value == 1
    await Timer(1, units="ns")


# --------------------------------------------------------------------------
# state machine
# --------------------------------------------------------------------------

async def tick(dut, n: int = 1) -> None:
    for _ in range(n):
        await RisingEdge(dut.clk)


async def arm(dut, *, settle: int, capture: int, slots: dict[int, int], ctrl_extra: int = 0,
              drain_timeout: int = 0, seq_count: int = 8) -> None:
    await axi_write(dut, REG["SETTLE_TICKS"], settle)
    await axi_write(dut, REG["CAPTURE_TICKS"], capture)
    await axi_write(dut, REG["DRAIN_TIMEOUT_TICKS"], drain_timeout)
    await axi_write(dut, REG["SEQ_SLOT_COUNT"], seq_count)
    for idx, hz_q44_20 in slots.items():
        await axi_write(dut, slot_lo(idx), hz_q44_20 & 0xFFFFFFFF)
        await axi_write(dut, slot_hi(idx), hz_q44_20 >> 32)
    await axi_write(dut, REG["CTRL_STATUS"], CTRL_ENABLE | CTRL_IDLE_OPEN | ctrl_extra)
    dut.fabric_samples_drained.value = 1  # a quiet framer: drained as soon as the gate shuts
    await tick(dut)


async def rise(dut, slot: int) -> None:
    """Rising edge on gpio[0] with the slot index on gpio[3:1]."""
    dut.scheduler_gpio.value = (slot << 1) | 1
    await tick(dut)


async def fall(dut) -> None:
    dut.scheduler_gpio.value = int(dut.scheduler_gpio.value) & 0b1110
    await tick(dut)


async def wait_state(dut, target: int, limit: int = 2000) -> int:
    """Return cycles elapsed until state_out == target."""
    for n in range(limit):
        await ReadOnly()
        if int(dut.state_out.value) == target:
            await Timer(1, units="ns")
            return n
        await RisingEdge(dut.clk)
    raise AssertionError(f"state {target} not reached within {limit} cycles")


async def framer_accepts_context(dut, ready_delay: int = 2, accept_delay: int = 5) -> None:
    """Model the framer: raise ready, see valid&ready, later pulse accepted."""
    await tick(dut, ready_delay)
    dut.fabric_context_ready.value = 1
    while True:
        await ReadOnly()
        if dut.fabric_context_valid.value and dut.fabric_context_ready.value:
            break
        await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.fabric_context_ready.value = 0
    await tick(dut, accept_delay)
    dut.fabric_context_accepted.value = 1
    await tick(dut)
    dut.fabric_context_accepted.value = 0


F2 = 2_440_000_000 << 20
F5 = 2_480_000_000 << 20


@cocotb.test()
async def event_runs_drain_settle_publish_open(dut):
    await reset(dut)
    await arm(dut, settle=10, capture=0, slots={2: F2, 5: F5})
    await ReadOnly()
    assert dut.stream_enable.value == 1, "enabled + idle_open: gate open in IDLE"
    await Timer(1, units="ns")

    await rise(dut, 2)
    await ReadOnly()
    assert int(dut.state_out.value) in (STATE["DRAIN"], STATE["SETTLE"])
    assert dut.stream_enable.value == 0, "gate shuts on the event"
    await Timer(1, units="ns")

    await wait_state(dut, STATE["PUBLISH"])
    await ReadOnly()
    assert dut.fabric_context_valid.value == 1
    assert int(dut.fabric_rf_frequency_q44_20.value) == F2
    assert int(dut.fabric_hop_epoch.value) == 1
    assert dut.stream_enable.value == 0, "still closed while Context is in flight"
    await Timer(1, units="ns")

    await framer_accepts_context(dut)
    await wait_state(dut, STATE["OPEN"])
    await ReadOnly()
    assert dut.stream_enable.value == 1
    assert dut.fabric_context_valid.value == 0
    await Timer(1, units="ns")

    assert await axi_read(dut, REG["EPOCH"]) == 1
    assert await axi_read(dut, REG["EVENT_COUNT"]) == 1
    assert await axi_read(dut, REG["LAST_SLOT"]) == 2
    assert await axi_read(dut, REG["SUPERSEDED_COUNT"]) == 0
    assert (await status(dut)) & (ST_SUPERSEDED | ST_DRAIN_TIMEOUT) == 0


@cocotb.test()
async def settle_ticks_is_exact(dut):
    await reset(dut)
    settle = 37
    await arm(dut, settle=settle, capture=0, slots={1: F2})
    await rise(dut, 1)
    n_settle = await wait_state(dut, STATE["SETTLE"])
    n_publish = await wait_state(dut, STATE["PUBLISH"])
    assert n_publish == settle + 1, f"SETTLE lasted {n_publish - 1} cycles, want {settle}"
    assert n_settle <= 2


@cocotb.test()
async def capture_ticks_closes_gate_after_exact_window(dut):
    await reset(dut)
    capture = 25
    await arm(dut, settle=3, capture=capture, slots={0: F2})
    await rise(dut, 0)
    await wait_state(dut, STATE["PUBLISH"])
    await framer_accepts_context(dut)
    await wait_state(dut, STATE["OPEN"])
    open_cycles = 0
    while True:
        await ReadOnly()
        if int(dut.state_out.value) != STATE["OPEN"]:
            break
        assert dut.stream_enable.value == 1
        open_cycles += 1
        await RisingEdge(dut.clk)
    assert int(dut.state_out.value) == STATE["CLOSED"]
    assert dut.stream_enable.value == 0
    await Timer(1, units="ns")
    assert open_cycles == capture, f"OPEN lasted {open_cycles}, want {capture}"
    assert await axi_read(dut, REG["CAPTURE_COUNT"]) == 1
    # Stays closed until the next event.
    await tick(dut, 50)
    assert await state_of(dut) == STATE["CLOSED"]
    await fall(dut)
    await rise(dut, 0)
    await wait_state(dut, STATE["DRAIN"], limit=4)


@cocotb.test()
async def capture_ticks_zero_stays_open_until_next_event(dut):
    await reset(dut)
    await arm(dut, settle=2, capture=0, slots={0: F2, 1: F5})
    await rise(dut, 0)
    await wait_state(dut, STATE["PUBLISH"])
    await framer_accepts_context(dut)
    await wait_state(dut, STATE["OPEN"])
    await tick(dut, 200)
    assert await state_of(dut) == STATE["OPEN"]
    assert await axi_read(dut, REG["CAPTURE_COUNT"]) == 0
    await fall(dut)
    await rise(dut, 1)
    await wait_state(dut, STATE["DRAIN"], limit=4)
    await ReadOnly()
    assert int(dut.fabric_rf_frequency_q44_20.value) == F5
    assert int(dut.fabric_hop_epoch.value) == 2
    await Timer(1, units="ns")


@cocotb.test()
async def event_during_settle_restarts_with_latest_slot_and_counts_superseded(dut):
    await reset(dut)
    await arm(dut, settle=40, capture=0, slots={2: F2, 5: F5})
    await rise(dut, 2)
    await wait_state(dut, STATE["SETTLE"])
    await tick(dut, 5)
    await fall(dut)
    await rise(dut, 5)                      # second hop before the first settled
    await wait_state(dut, STATE["PUBLISH"])
    await ReadOnly()
    assert int(dut.fabric_rf_frequency_q44_20.value) == F5, "latest slot wins"
    assert int(dut.fabric_hop_epoch.value) == 2, "epoch counts every event"
    await Timer(1, units="ns")
    await framer_accepts_context(dut)
    await wait_state(dut, STATE["OPEN"])
    assert await axi_read(dut, REG["SUPERSEDED_COUNT"]) == 1
    assert await axi_read(dut, REG["EVENT_COUNT"]) == 2
    st = await status(dut)
    assert st & ST_SUPERSEDED
    await axi_write(dut, REG["CTRL_STATUS"], CTRL_ENABLE | CTRL_IDLE_OPEN | CTRL_W1C_CLEAR)
    assert (await status(dut)) & ST_SUPERSEDED == 0
    assert await axi_read(dut, REG["SUPERSEDED_COUNT"]) == 0
    assert await state_of(dut) == STATE["OPEN"], "W1C does not disturb the gate"


@cocotb.test()
async def event_after_handshake_republishes_without_opening(dut):
    await reset(dut)
    await arm(dut, settle=2, capture=0, slots={2: F2, 5: F5})
    await rise(dut, 2)
    await wait_state(dut, STATE["PUBLISH"])
    # Framer takes the tuple (valid&ready) but has not finished sending it.
    dut.fabric_context_ready.value = 1
    await tick(dut, 2)
    dut.fabric_context_ready.value = 0
    await ReadOnly()
    assert dut.fabric_context_valid.value == 0, "handshake consumed valid"
    await Timer(1, units="ns")
    await fall(dut)
    await rise(dut, 5)                      # stale Context is in flight
    await tick(dut, 3)
    dut.fabric_context_accepted.value = 1
    await tick(dut)
    dut.fabric_context_accepted.value = 0
    # Must re-run the cycle for epoch 2, never open in between.
    for _ in range(60):
        await ReadOnly()
        assert dut.stream_enable.value == 0, "gate opened between stale and fresh Context"
        if int(dut.state_out.value) == STATE["PUBLISH"] and dut.fabric_context_valid.value:
            break
        await RisingEdge(dut.clk)
    else:
        raise AssertionError("second PUBLISH never reached")
    assert int(dut.fabric_rf_frequency_q44_20.value) == F5
    assert int(dut.fabric_hop_epoch.value) == 2
    await Timer(1, units="ns")
    await framer_accepts_context(dut)
    await wait_state(dut, STATE["OPEN"])
    assert await axi_read(dut, REG["SUPERSEDED_COUNT"]) == 1


@cocotb.test()
async def sequence_slot_mode_advances_and_wraps(dut):
    await reset(dut)
    await arm(dut, settle=1, capture=0, slots={0: 10 << 20, 1: 11 << 20, 2: 12 << 20},
              ctrl_extra=CTRL_SLOT_SOURCE_SEQ, seq_count=3)
    seen = []
    for _ in range(4):
        await rise(dut, 7)                  # gpio slot bits must be ignored
        await wait_state(dut, STATE["PUBLISH"])
        await ReadOnly()
        seen.append(int(dut.fabric_rf_frequency_q44_20.value) >> 20)
        await Timer(1, units="ns")
        await framer_accepts_context(dut)
        await wait_state(dut, STATE["OPEN"])
        await fall(dut)
    assert seen == [10, 11, 12, 10]
    assert await axi_read(dut, REG["SEQ_INDEX"]) == 1
    assert await axi_read(dut, REG["LAST_SLOT"]) == 0
    await axi_write(dut, REG["SEQ_INDEX"], 0)
    assert await axi_read(dut, REG["SEQ_INDEX"]) == 0


@cocotb.test()
async def drain_timeout_proceeds_and_sets_sticky(dut):
    await reset(dut)
    await arm(dut, settle=1, capture=0, slots={0: F2}, drain_timeout=12)
    dut.fabric_samples_drained.value = 0    # framer never reports drained
    await rise(dut, 0)
    n = await wait_state(dut, STATE["SETTLE"], limit=40)
    assert 12 <= n <= 15, n
    assert (await status(dut)) & ST_DRAIN_TIMEOUT
    await axi_write(dut, REG["CTRL_STATUS"], CTRL_ENABLE | CTRL_IDLE_OPEN | CTRL_W1C_CLEAR)
    assert (await status(dut)) & ST_DRAIN_TIMEOUT == 0


@cocotb.test()
async def drain_without_timeout_waits_forever(dut):
    await reset(dut)
    await arm(dut, settle=1, capture=0, slots={0: F2}, drain_timeout=0)
    dut.fabric_samples_drained.value = 0
    await rise(dut, 0)
    await tick(dut, 300)
    assert await state_of(dut) == STATE["DRAIN"]
    dut.fabric_samples_drained.value = 1
    await wait_state(dut, STATE["SETTLE"], limit=4)


@cocotb.test()
async def idle_open_zero_keeps_gate_closed_until_first_capture(dut):
    await reset(dut)
    await axi_write(dut, REG["SETTLE_TICKS"], 1)
    await axi_write(dut, REG["CTRL_STATUS"], CTRL_ENABLE)      # idle_open = 0
    await tick(dut)
    await ReadOnly()
    assert dut.stream_enable.value == 0
    await Timer(1, units="ns")
    dut.fabric_samples_drained.value = 1
    await rise(dut, 0)
    await wait_state(dut, STATE["PUBLISH"])
    await framer_accepts_context(dut)
    await wait_state(dut, STATE["OPEN"])
    await ReadOnly()
    assert dut.stream_enable.value == 1
    await Timer(1, units="ns")


@cocotb.test()
async def disable_mid_flight_returns_idle_and_opens_gate(dut):
    await reset(dut)
    await arm(dut, settle=500, capture=0, slots={0: F2})
    await rise(dut, 0)
    await wait_state(dut, STATE["SETTLE"])
    await axi_write(dut, REG["CTRL_STATUS"], CTRL_IDLE_OPEN)   # enable = 0
    await tick(dut)
    await ReadOnly()
    assert int(dut.state_out.value) == STATE["IDLE"]
    assert dut.stream_enable.value == 1
    assert dut.fabric_context_valid.value == 0
    await Timer(1, units="ns")
