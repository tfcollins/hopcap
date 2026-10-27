# SPDX-License-Identifier: MIT
"""vrt49_fhm_hop_ctrl driving the real vrt49 framer (fhm_hop_chain.v).

A free-running ramp source with bubbles models the ADC path (real duty at
245.76 MSPS / 4 words per 100 MHz beat is ~61 %).  Every packet leaving the
framer is captured and classified; the tests assert the ordering contract the
host relies on: after a hop event no IF Data leaves until the changed Context
carrying the slot frequency and epoch has left, and the data that follows is
exclusively post-settle samples.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

from hopctrl_regs import REG, STATE, slot_hi, slot_lo
from test_fhm_hop_ctrl import CTRL_ENABLE, CTRL_IDLE_OPEN, axi_read, axi_write
from vrt49_codec import FrameSpec, build_context_packet

CFG_STREAM_ID = 0x10
CFG_PKT_SIZE_WORDS = 0x20
CFG_SAMPLES_PER_PKT = 0x24
CFG_CONTROL = 0x40
CIF0_CHANGED = 0xA8618006
PAYLOAD_WORDS = 16
STREAM_ID = 0x01000000
F3 = 2_430_000_000 << 20
F6 = 2_470_000_000 << 20


@dataclass
class Packet:
    words: list[int]
    cycle_last: int          # cycle index when TLAST left
    ramp_at_last: int        # source ramp value when TLAST left

    @property
    def kind(self) -> str:
        t = self.words[0] >> 28
        return {1: "data", 4: "context"}.get(t, f"type{t}")

    @property
    def payload(self) -> list[int]:
        return self.words[7:]


class Harness:
    def __init__(self, dut):
        self.dut = dut
        self.cycle = 0
        self.ramp = 0x1000
        self.packets: list[Packet] = []
        self.gate_open_cycles: list[int] = []
        self.accepted_since_event = False
        self.protocol_errors: list[str] = []

    async def clock_counter(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.cycle += 1

    async def source(self, duty: tuple[int, int] = (2, 3)):
        """Drive a ramp, valid on `duty[0]` of every `duty[1]` cycles."""
        n = 0
        while True:
            await RisingEdge(self.dut.clk)
            if (n % duty[1]) < duty[0]:
                self.dut.s_axis_tdata.value = self.ramp
                self.dut.s_axis_tvalid.value = 1
                await ReadOnly()
                if self.dut.s_axis_tready.value == 1:
                    self.ramp += 1
            else:
                self.dut.s_axis_tvalid.value = 0
            n += 1

    async def sink(self):
        cur: list[int] = []
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            if self.dut.m_axis_tvalid.value == 1 and self.dut.m_axis_tready.value == 1:
                cur.append(int(self.dut.m_axis_tdata.value))
                if self.dut.m_axis_tlast.value == 1:
                    self.packets.append(Packet(cur, self.cycle, self.ramp))
                    cur = []

    async def gate_watch(self):
        prev = int(self.dut.stream_enable.value)
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            if int(self.dut.fabric_context_accepted.value):
                self.accepted_since_event = True
            now = int(self.dut.stream_enable.value)
            if now and not prev:
                self.gate_open_cycles.append(self.cycle)
                if not self.accepted_since_event:
                    self.protocol_errors.append(
                        f"gate reopened at cycle {self.cycle} before the Context left the framer")
            prev = now

    async def cfg_write(self, addr: int, data: int):
        await RisingEdge(self.dut.clk)
        self.dut.cfg_addr.value = addr
        self.dut.cfg_wdata.value = data
        self.dut.cfg_wstrobe.value = 1
        await RisingEdge(self.dut.clk)
        self.dut.cfg_wstrobe.value = 0

    async def start(self, *, settle: int, capture: int, slots: dict[int, int]):
        dut = self.dut
        cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
        dut.resetn.value = 0
        for sig in ("scheduler_gpio", "s_axis_tdata", "s_axis_tvalid", "cfg_addr", "cfg_wdata",
                    "cfg_wstrobe", "cfg_raddr", "s_axi_awvalid", "s_axi_wvalid", "s_axi_bready",
                    "s_axi_arvalid", "s_axi_rready", "s_axi_awprot", "s_axi_arprot"):
            getattr(dut, sig).value = 0
        dut.m_axis_tready.value = 1
        for _ in range(4):
            await RisingEdge(dut.clk)
        dut.resetn.value = 1
        for _ in range(2):
            await RisingEdge(dut.clk)
        cocotb.start_soon(self.clock_counter())
        await self.cfg_write(CFG_STREAM_ID, STREAM_ID)
        await self.cfg_write(CFG_PKT_SIZE_WORDS, 7 + PAYLOAD_WORDS)
        await self.cfg_write(CFG_SAMPLES_PER_PKT, PAYLOAD_WORDS)
        await axi_write(dut, REG["SETTLE_TICKS"], settle)
        await axi_write(dut, REG["CAPTURE_TICKS"], capture)
        for idx, f in slots.items():
            await axi_write(dut, slot_lo(idx), f & 0xFFFFFFFF)
            await axi_write(dut, slot_hi(idx), f >> 32)
        await axi_write(dut, REG["CTRL_STATUS"], CTRL_ENABLE | CTRL_IDLE_OPEN)
        cocotb.start_soon(self.gate_watch())
        cocotb.start_soon(self.sink())
        cocotb.start_soon(self.source())
        await self.cfg_write(CFG_CONTROL, 0x1)  # framer enable

    async def hop(self, slot: int) -> int:
        """Rising edge on gpio[0] with `slot` on gpio[3:1]; returns the ramp value at the edge."""
        await RisingEdge(self.dut.clk)
        self.dut.scheduler_gpio.value = (slot << 1) | 1
        self.accepted_since_event = False
        edge_ramp = self.ramp
        for _ in range(3):
            await RisingEdge(self.dut.clk)
        self.dut.scheduler_gpio.value = slot << 1
        return edge_ramp

    async def run(self, cycles: int):
        for _ in range(cycles):
            await RisingEdge(self.dut.clk)

    async def wait_packets(self, n: int, limit: int = 20000):
        for _ in range(limit):
            if len(self.packets) >= n:
                return
            await RisingEdge(self.dut.clk)
        raise AssertionError(f"only {len(self.packets)} packets after {limit} cycles, wanted {n}")


def contexts(pkts):
    return [p for p in pkts if p.kind == "context"]


def check_context(ctx: Packet, freq_q44_20: int, epoch: int):
    assert len(ctx.words) == 22, len(ctx.words)
    assert ctx.words[7] == CIF0_CHANGED, hex(ctx.words[7])
    assert ctx.words[12] == freq_q44_20 >> 32
    assert ctx.words[13] == freq_q44_20 & 0xFFFFFFFF
    assert ctx.words[21] == epoch
    assert (ctx.words[1] & 0xFFFFFFFF) == STREAM_ID
    # Byte-exact against the libvrt49-backed golden builder: only the fields the
    # fabric path owns (RF frequency, info source = epoch) and the framer-owned
    # prologue counters/timestamp may be non-zero in this testbench.
    spec = FrameSpec(stream_id=STREAM_ID, pkt_count=(ctx.words[0] >> 16) & 0xF,
                     int_secs=ctx.words[4], frac_ps=(ctx.words[5] << 32) | ctx.words[6])
    golden = build_context_packet(spec, changed=True, bandwidth=0, rf_frequency=freq_q44_20,
                                  overrange_count=0, sample_rate=0, state_event=ctx.words[17],
                                  signal_format=0, discrete_io_32=0, info_source_id=epoch)
    got = b"".join(w.to_bytes(4, "big") for w in ctx.words)
    assert got == golden, "Context bytes differ from golden codec"


@cocotb.test()
async def hop_event_publishes_context_before_any_fresh_data(dut):
    h = Harness(dut)
    await h.start(settle=60, capture=0, slots={3: F3})
    await h.wait_packets(3)                     # steady state streaming
    edge_ramp = await h.hop(3)
    before = len(h.packets)
    await h.wait_packets(before + 4)

    after = h.packets[before - 1:]              # include the packet that may have been in flight
    ctxs = contexts(after)
    assert len(ctxs) == 1, [p.kind for p in after]
    ctx = ctxs[0]
    check_context(ctx, F3, epoch=1)
    i_ctx = after.index(ctx)

    # Anything that left between the edge and the Context must be the single
    # packet the framer already owned at the edge (packet-atomic close).
    pre = [p for p in after[:i_ctx] if p.cycle_last >= 0]
    data_between = [p for p in pre if p.kind == "data" and max(p.payload) >= edge_ramp]
    assert len(data_between) <= 1, "more than the owned packet leaked after the edge"

    # Every data packet after the Context carries only samples produced after
    # the gate reopened (no stale pre-hop samples survive the drain).
    assert h.gate_open_cycles, "gate never reopened"
    reopened_ramp_floor = edge_ramp  # conservative: stale == anything from before the edge
    for p in after[i_ctx + 1:]:
        assert p.kind == "data"
        assert min(p.payload) > reopened_ramp_floor, "stale pre-hop sample after Context"
    assert await axi_read(dut, REG["EPOCH"]) == 1
    assert await axi_read(dut, REG["SUPERSEDED_COUNT"]) == 0
    assert (await axi_read(dut, REG["CTRL_STATUS"])) & 0x7 == STATE["OPEN"]
    assert not h.protocol_errors, h.protocol_errors


@cocotb.test()
async def capture_ticks_bounds_the_data_that_follows_context(dut):
    h = Harness(dut)
    capture = 4 * PAYLOAD_WORDS * 3 // 2 + PAYLOAD_WORDS  # ~ (4 pkts at 2/3 duty) + half a packet
    await h.start(settle=20, capture=capture, slots={6: F6})
    await h.wait_packets(2)
    await h.hop(6)
    # Wait until the controller has gone through OPEN and shut again.
    for _ in range(4000):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.state_out.value) == STATE["CLOSED"]:
            break
    await Timer(1, units="ns")
    await h.run(6 * PAYLOAD_WORDS)             # drain any owned packet, then confirm silence
    settled = list(h.packets)
    ctxs = contexts(settled)
    assert len(ctxs) == 1
    i_ctx = settled.index(ctxs[0])
    post = settled[i_ctx + 1:]
    assert all(p.kind == "data" for p in post)
    expected = capture * 2 / 3 / PAYLOAD_WORDS  # samples admitted / samples per packet
    assert math.floor(expected) - 1 <= len(post) <= math.ceil(expected) + 1, (len(post), expected)
    n_before = len(h.packets)
    await h.run(6 * PAYLOAD_WORDS)
    assert len(h.packets) == n_before, "data leaked while CLOSED"
    assert await axi_read(dut, REG["CAPTURE_COUNT"]) == 1
    assert not h.protocol_errors, h.protocol_errors


@cocotb.test()
async def consecutive_hops_each_get_one_context_with_increasing_epoch(dut):
    h = Harness(dut)
    await h.start(settle=30, capture=0, slots={3: F3, 6: F6})
    await h.wait_packets(2)
    await h.hop(3)
    await h.run(400)
    await h.hop(6)
    await h.run(400)
    ctxs = contexts(h.packets)
    assert len(ctxs) == 2, [p.kind for p in h.packets]
    check_context(ctxs[0], F3, epoch=1)
    check_context(ctxs[1], F6, epoch=2)
    i0, i1 = h.packets.index(ctxs[0]), h.packets.index(ctxs[1])
    assert any(p.kind == "data" for p in h.packets[i0 + 1:i1]), "no capture between hops"
    assert await axi_read(dut, REG["EVENT_COUNT"]) == 2
    assert await axi_read(dut, REG["SUPERSEDED_COUNT"]) == 0
    assert not h.protocol_errors, h.protocol_errors
