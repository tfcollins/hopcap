# SPDX-License-Identifier: MIT
"""Unit tests for tick_pps_select: external 1PPS qualification, fallback to Corundum's PTP PPS,
and the no-spurious-edge guarantee on Tick's pps_in.

The RTL is built with small cycle counts (Makefile TOP=tick_pps_select passes them with -G and
exports them as PPS_SEL_*): a "second" is NOMINAL cycles.  Inputs are driven on the falling
edge from precomputed per-cycle level arrays, and every cycle's outputs are recorded, so each
test checks the whole trace against the invariants:

  * every pps_out rising edge is a rising edge of the then-selected input plus a fixed latency;
  * no pps_out edge comes from the unselected input;
  * consecutive pps_out edges are at least PERIOD_MIN cycles apart;
  * at most one selected-input edge is dropped per switch.
"""

from __future__ import annotations

import os
import random
from itertools import pairwise

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

ALIVE_TIMEOUT = int(os.environ.get("PPS_SEL_ALIVE_TIMEOUT", "250"))
PERIOD_MIN = int(os.environ.get("PPS_SEL_PERIOD_MIN", "180"))
PERIOD_MAX = int(os.environ.get("PPS_SEL_PERIOD_MAX", "220"))
GOOD_PERIODS = int(os.environ.get("PPS_SEL_GOOD_PERIODS", "2"))
PULSE_CYCLES = int(os.environ.get("PPS_SEL_PULSE_CYCLES", "20"))
NOMINAL = 200
INT_HIGH = 108  # Corundum's ptp_pps_str is high ~0.54 s of each second
EXT_HIGH = 40

# Input level change at the falling edge of cycle n -> meta, sync, detect, pps_out register.
LATENCY = 3


def periodic(
    n: int, period: int, high: int, phase: int = 0, start: int = 0, stop: int | None = None
):
    lv = bytearray(n)
    t = phase
    while t < n:
        if t >= start and (stop is None or t < stop):
            for k in range(t, min(t + high, n)):
                lv[k] = 1
        t += period
    return lv


def from_edges(n: int, edges, high: int):
    lv = bytearray(n)
    for t in edges:
        for k in range(max(t, 0), min(t + high, n)):
            lv[k] = 1
    return lv


def rises(lv) -> list[int]:
    return [i for i in range(1, len(lv)) if lv[i] and not lv[i - 1]]


class Trace:
    def __init__(self, ext, int_, out, sel, alive):
        self.ext, self.int, self.out, self.sel, self.alive = ext, int_, out, sel, alive

    @property
    def out_edges(self) -> list[int]:
        return rises(self.out)

    def switches(self) -> list[int]:
        return [i for i in range(1, len(self.sel)) if self.sel[i] != self.sel[i - 1]]

    def ext_period(self, e: int, ext_r: list[int]) -> int | None:
        """Period closed by external edge e (cycles from the previous external edge)."""
        i = ext_r.index(e)
        return e - ext_r[i - 1] if i else None

    def ext_in_window(self, e: int, ext_r: list[int]) -> bool:
        p = self.ext_period(e, ext_r)
        return p is not None and PERIOD_MIN <= p <= PERIOD_MAX

    def check(self) -> dict:
        """Assert the invariants; return counts for test-specific checks."""
        ext_l = rises(self.ext)
        ext_r, int_r = set(ext_l), set(rises(self.int))
        outs = self.out_edges
        for a, b in pairwise(outs):
            assert b - a >= PERIOD_MIN, f"output edges {a} and {b} only {b - a} cycles apart"
        for o in outs:
            src = self.sel[o - 1]  # selection in force when the edge was accepted
            edge = o - LATENCY
            mine, other = (ext_r, int_r) if src else (int_r, ext_r)
            assert edge in mine, (
                f"output edge at {o} has no {'ext' if src else 'int'} edge at {edge} "
                f"(selected={src}; other input edge there: {edge in other})"
            )
            if src:
                assert self.ext_in_window(edge, ext_l), (
                    f"external edge {edge} reached Tick with period "
                    f"{self.ext_period(edge, ext_l)} outside [{PERIOD_MIN}, {PERIOD_MAX}]"
                )
        # Selected-input edges that reached the detector inside the trace and that the
        # selector may deliver: every internal edge, external ones only with in-window periods.
        offered = 0
        for e in sorted(ext_r | int_r):
            d = e + LATENCY - 1  # cycle whose sel decided acceptance
            if d >= len(self.sel):
                continue
            ext_ok = e in ext_r and self.sel[d] and self.ext_in_window(e, ext_l)
            int_ok = e in int_r and not self.sel[d]
            offered += ext_ok or int_ok
        # Switch rule: a switch recorded at s was decided in the cycle before it, where the
        # synchronised level is the input driven at s-LATENCY and the previous sample the one
        # before.  The target input must be quiet there.
        for s in self.switches():
            tgt = self.ext if self.sel[s] else self.int
            window = range(s - LATENCY - 1, s - LATENCY + 1)
            assert not any(tgt[k] for k in window), f"switch at {s} with target input high"
        dropped = offered - len(outs)
        assert dropped >= 0
        assert dropped <= len(self.switches()), (
            f"{dropped} in-window selected edges dropped across {len(self.switches())} switches"
        )
        return {"outs": len(outs), "offered": offered, "dropped": dropped}


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.ext_pps_async.value = 0
    dut.int_pps_async.value = 0
    dut.resetn.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.resetn.value = 1


async def run(dut, ext, int_, *, reset_first=True, resets=()) -> Trace:
    """Drive ext/int level arrays (one entry per cycle) and record outputs each cycle."""
    if reset_first:
        dut.resetn.value = 0
        dut.ext_pps_async.value = ext[0]
        dut.int_pps_async.value = int_[0]
        for _ in range(3):
            await RisingEdge(dut.clk)
    n = len(ext)
    out, sel, alive = bytearray(n), bytearray(n), bytearray(n)
    for i in range(n):
        await FallingEdge(dut.clk)
        dut.resetn.value = 0 if i in resets else 1
        dut.ext_pps_async.value = ext[i]
        dut.int_pps_async.value = int_[i]
        out[i] = int(dut.pps_out.value)
        sel[i] = int(dut.sel_ext.value)
        alive[i] = int(dut.ext_alive.value)
    return Trace(ext, int_, out, sel, alive)


@cocotb.test()
async def test_internal_only(dut):
    """No external: every Corundum edge reaches Tick, one for one, internal always selected."""
    await start(dut)
    n = 12 * NOMINAL
    int_ = periodic(n, NOMINAL, INT_HIGH, phase=37)
    tr = await run(dut, bytearray(n), int_)
    assert not any(tr.sel) and not any(tr.alive)
    assert tr.out_edges == [e + LATENCY for e in rises(int_)]
    tr.check()


@cocotb.test()
async def test_external_selected_after_two_good_periods(dut):
    """Good external: alive only after GOOD_PERIODS periods, then edges come from external."""
    await start(dut)
    n = 14 * NOMINAL
    int_ = periodic(n, NOMINAL, INT_HIGH, phase=20)
    ext = periodic(n, NOMINAL, EXT_HIGH, phase=150)
    tr = await run(dut, ext, int_)
    er = rises(ext)
    # alive rises right after the (GOOD_PERIODS+1)-th edge, i.e. after GOOD_PERIODS periods
    first_alive = tr.alive.index(1)
    qualifying = er[GOOD_PERIODS]
    assert qualifying < first_alive <= qualifying + LATENCY + 2, (first_alive, er[:4])
    assert not any(tr.alive[: er[GOOD_PERIODS - 1] + LATENCY + 2])
    assert tr.sel[-1] == 1 and len(tr.switches()) == 1
    sw = tr.switches()[0]
    after = [o for o in tr.out_edges if o > sw + NOMINAL]
    assert after and all(o - LATENCY in set(er) for o in after)
    stats = tr.check()
    assert stats["dropped"] <= 1


@cocotb.test()
async def test_jittered_in_range_is_selected(dut):
    """Periods randomly inside [PERIOD_MIN, PERIOD_MAX] still qualify."""
    await start(dut)
    rng = random.Random(7)
    n = 16 * NOMINAL
    edges, t = [], 60
    while t < n:
        edges.append(t)
        t += rng.randint(PERIOD_MIN + 2, PERIOD_MAX - 2)
    tr = await run(dut, from_edges(n, edges, EXT_HIGH), periodic(n, NOMINAL, INT_HIGH, phase=5))
    assert tr.sel[-1] == 1
    tr.check()


def assert_fallback(tr: Trace, last: int) -> int:
    """External's last edge was at `last`: check the exact fallback instant and that Tick gets
    no edge until the first internal edge after it, then every internal edge.  Returns the
    number of Tick seconds lost."""
    n = len(tr.sel)
    drop = next(i for i in range(last, n) if tr.alive[i - 1] and not tr.alive[i])
    # the edge is counted at last+LATENCY; ALIVE_TIMEOUT cycles on, the registered alive clears
    assert drop == last + LATENCY + 1 + ALIVE_TIMEOUT, (drop, last)
    # the switch happens in the first cycle whose synchronised internal samples are both low
    sw = next(
        s for s in range(drop + 1, n) if not tr.int[s - LATENCY] and not tr.int[s - LATENCY - 1]
    )
    assert tr.switches()[-1] == sw and tr.sel[-1] == 0, (tr.switches(), sw)
    assert sw - drop <= INT_HIGH + LATENCY + 1
    outs = tr.out_edges
    assert [o for o in outs if last + LATENCY < o < sw] == []
    expect = [r + LATENCY for r in rises(tr.int) if r + LATENCY - 1 >= sw]
    assert [o for o in outs if o >= sw] == expect
    gap = expect[0] - (last + LATENCY)
    assert ALIVE_TIMEOUT < gap <= ALIVE_TIMEOUT + NOMINAL + 2 * LATENCY, gap
    lost = round(gap / NOMINAL) - 1
    assert lost in (1, 2), gap
    return lost


@cocotb.test()
async def test_external_stops_falls_back_to_internal(dut):
    """External stops low: alive drops ALIVE_TIMEOUT after its last edge, Tick falls back at
    the first quiet internal sample; 1-2 Tick seconds are lost, none duplicated."""
    await start(dut)
    n = 18 * NOMINAL
    stop = 8 * NOMINAL
    for int_phase in (11, 120):
        int_ = periodic(n, NOMINAL, INT_HIGH, phase=int_phase)
        ext = periodic(n, NOMINAL, EXT_HIGH, phase=90, stop=stop)
        tr = await run(dut, ext, int_)
        assert len(tr.switches()) == 2
        assert_fallback(tr, rises(ext)[-1])
        tr.check()


@cocotb.test()
async def test_external_stuck_high_still_falls_back(dut):
    """External dies HIGH: the fallback is exactly as fast as for a pin that stops low."""
    await start(dut)
    n = 18 * NOMINAL
    stop = 8 * NOMINAL
    for int_phase in (11, 120):
        int_ = periodic(n, NOMINAL, INT_HIGH, phase=int_phase)
        ext = periodic(n, NOMINAL, EXT_HIGH, phase=90, stop=stop)
        last = rises(ext)[-1]
        for k in range(last, n):
            ext[k] = 1
        tr = await run(dut, ext, int_)
        assert len(tr.switches()) == 2
        assert_fallback(tr, last)
        tr.check()


@cocotb.test()
async def test_late_external_edge_never_reaches_tick(dut):
    """Selected external, one period of PERIOD_MAX < p < ALIVE_TIMEOUT: still 'alive' when the
    late edge arrives, but the edge is refused in that cycle (review I1, repro mode=1)."""
    await start(dut)
    n = 16 * NOMINAL
    int_ = periodic(n, NOMINAL, INT_HIGH, phase=30)
    late = PERIOD_MAX + 15
    assert late < ALIVE_TIMEOUT
    edges = [10 + k * NOMINAL for k in range(6)]
    edges += [edges[-1] + late + k * NOMINAL for k in range(10)]  # runs to the end
    ext = from_edges(n, edges, EXT_HIGH)
    tr = await run(dut, ext, int_)
    le = edges[6]
    assert tr.sel[le + LATENCY - 1] == 1 and tr.alive[le + LATENCY - 1] == 1, "premise"
    assert le + LATENCY not in tr.out_edges
    assert tr.sel[-1] == 1  # requalifies
    tr.check()


@cocotb.test()
async def test_glitch_after_a_dropped_edge_never_reaches_tick(dut):
    """The first external edge after a switch is dropped by the spacing guard; a glitch more
    than PERIOD_MIN after the previous output edge (but only part of a second after that
    dropped edge) must not reach Tick either (review I1, repro mode=0)."""
    await start(dut)
    n = 14 * NOMINAL
    # Internal fires 30 cycles after each external edge, so the last internal edge before the
    # switch leaves the next external edge inside the spacing window.
    int_ = periodic(n, NOMINAL, INT_HIGH, phase=90)
    ext = periodic(n, NOMINAL, EXT_HIGH, phase=60)
    dropped_edge = 60 + 3 * NOMINAL  # first external edge after the switch to external
    glitch = dropped_edge + 130
    ext[glitch] = ext[glitch + 1] = 1
    tr = await run(dut, ext, int_)
    d = dropped_edge + LATENCY - 1
    assert tr.sel[d] == 1, "premise: external selected when its edge arrives"
    assert dropped_edge + LATENCY not in tr.out_edges, "premise: spacing guard dropped it"
    prev_out = max(o for o in tr.out_edges if o < dropped_edge)
    assert glitch - prev_out >= PERIOD_MIN, "premise: the spacing guard alone would pass it"
    assert glitch + LATENCY not in tr.out_edges
    tr.check()


@cocotb.test()
async def test_out_of_range_periods_never_selected(dut):
    """Fast, slow, alternating and jittered-out-of-range externals never qualify."""
    await start(dut)
    n = 14 * NOMINAL
    int_ = periodic(n, NOMINAL, INT_HIGH, phase=30)
    rng = random.Random(3)
    cases = {
        "fast": periodic(n, PERIOD_MIN - 3, EXT_HIGH, phase=10),
        "slow": periodic(n, PERIOD_MAX + 3, EXT_HIGH, phase=10),
    }
    alt, t, k = [], 10, 0
    while t < n:  # one good period between bad ones: never GOOD_PERIODS in a row
        alt.append(t)
        t += NOMINAL if k % 2 == 0 else PERIOD_MAX + 15
        k += 1
    cases["alternating"] = from_edges(n, alt, EXT_HIGH)
    jit, t = [], 10
    while t < n:
        jit.append(t)
        t += rng.choice([PERIOD_MIN - rng.randint(1, 30), PERIOD_MAX + rng.randint(1, 30)])
    cases["jitter_out"] = from_edges(n, jit, EXT_HIGH)
    for name, ext in cases.items():
        tr = await run(dut, ext, int_)
        assert not any(tr.alive), name
        assert not any(tr.sel), name
        assert tr.out_edges == [e + LATENCY for e in rises(int_)], name
        tr.check()


@cocotb.test()
async def test_glitchy_or_floating_pin_never_selected(dut):
    """Chatter, a floating pin (random levels) and good edges with glitches: never selected."""
    await start(dut)
    n = 14 * NOMINAL
    int_ = periodic(n, NOMINAL, INT_HIGH, phase=0)
    rng = random.Random(11)
    floating = bytearray(rng.random() < 0.5 for _ in range(n))
    chatter = periodic(n, 7, 3)
    glitchy = periodic(n, NOMINAL, EXT_HIGH, phase=40)
    for t in range(40 + 90, n, NOMINAL):  # a 2-cycle glitch 90 cycles after every edge
        glitchy[t] = glitchy[t + 1] = 1
    for name, ext in {"floating": floating, "chatter": chatter, "glitchy": glitchy}.items():
        tr = await run(dut, ext, int_)
        assert not any(tr.alive), name
        assert not any(tr.sel), name
        assert tr.out_edges == [e + LATENCY for e in rises(int_)], name
        tr.check()


@cocotb.test()
async def test_glitch_on_selected_external_never_reaches_tick(dut):
    """Once external is selected, a glitch edge is dropped (spacing guard) and selection falls
    back; Tick sees no edge closer than PERIOD_MIN."""
    await start(dut)
    n = 18 * NOMINAL
    int_ = periodic(n, NOMINAL, INT_HIGH, phase=120)
    ext = periodic(n, NOMINAL, EXT_HIGH, phase=10)
    g = 10 + 7 * NOMINAL + 70
    ext[g] = ext[g + 1] = 1
    tr = await run(dut, ext, int_)
    assert tr.sel[g] == 1, "external should be selected before the glitch"
    assert g + LATENCY not in tr.out_edges
    assert tr.sel[-1] == 1  # requalifies after GOOD_PERIODS more good periods
    assert 0 in tr.sel[g:], "glitch must clear the qualification and fall back"
    tr.check()


@cocotb.test()
async def test_switch_phase_sweep_no_spurious_or_duplicate_edges(dut):
    """Sweep the external/internal phase (incl. near-coincident edges and a 50 % external in
    anti-phase with Corundum's 54 % level); external runs, then stops.  Count edges."""
    await start(dut)
    n = 20 * NOMINAL
    stop = 9 * NOMINAL
    phases = [0, 1, 2, 3, 197, 198, 199, 25, 50, 75, 100, 107, 108, 109, 125, 150, 175]
    for duty in (EXT_HIGH, NOMINAL // 2):
        for ph in phases:
            int_ = periodic(n, NOMINAL, INT_HIGH, phase=0)
            ext = periodic(n, NOMINAL, duty, phase=ph + 3, stop=stop)
            tr = await run(dut, ext, int_)
            assert len(tr.switches()) == 2, (duty, ph, tr.switches())
            stats = tr.check()
            # one edge per nominal second, minus at most one per switch
            seconds = len(rises(int_))
            assert seconds - 3 <= stats["outs"] <= seconds + 1, (duty, ph, stats, seconds)
    # Anti-phase 50 % external: there is never a cycle with both inputs low.
    int_ = periodic(n, NOMINAL, NOMINAL // 2, phase=0)
    ext = periodic(n, NOMINAL, NOMINAL // 2, phase=NOMINAL // 2)
    tr = await run(dut, ext, int_)
    assert not any(not e and not i for e, i in zip(ext[NOMINAL:], int_[NOMINAL:], strict=True))
    assert tr.sel[-1] == 1, "switch must complete even without a both-low window"
    tr.check()


@cocotb.test()
async def test_reset_defaults_and_no_edge_after_reset(dut):
    """Reset selects internal, clears qualification, and emits no edge for inputs already high."""
    await start(dut)
    n = 14 * NOMINAL
    int_ = periodic(n, NOMINAL, INT_HIGH, phase=0)
    ext = periodic(n, NOMINAL, EXT_HIGH, phase=0)
    # both inputs high at reset release (cycle 0): first output edge must be a later real edge
    tr = await run(dut, ext, int_)
    assert tr.out[0] == 0 and tr.sel[0] == 0 and tr.alive[0] == 0
    assert tr.out_edges[0] == NOMINAL + LATENCY
    assert tr.sel[-1] == 1
    tr.check()
    # reset while external is selected: back to internal, qualification starts again
    r = 3 * NOMINAL + 60
    int2 = periodic(n, NOMINAL, INT_HIGH, phase=50)
    ext2 = periodic(n, NOMINAL, EXT_HIGH, phase=0)
    tr2 = await run(dut, ext2, int2, resets=range(r, r + 3))
    assert tr2.sel[r - 1] == 1
    assert tr2.sel[r + 3] == 0 and tr2.alive[r + 3] == 0 and tr2.out[r + 3] == 0
    assert tr2.sel[-1] == 1
    later = [o for o in tr2.out_edges if o > r]
    assert all(b - a >= PERIOD_MIN for a, b in pairwise(later))
