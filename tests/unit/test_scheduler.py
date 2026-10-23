# SPDX-License-Identifier: MIT
"""TickSession against a fake IIO device backed by tick.hdl_model with a virtual clock."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import FakeStager, FakeTickDevice

from hopcap.plan import FhmPipelineModel, HopPlan, compile_plan
from hopcap.scheduler import TickSession

TICK_HZ = 100_000_000
MHZ = 1_000_000


def plan(n=6):
    # cycle four in-band frequencies so long plans stay within the 8-slot table
    freqs = tuple(2_410 * MHZ + (i % 4) * 10 * MHZ for i in range(n))
    return HopPlan(
        frequencies_hz=freqs,
        period_ticks=2_000_000,
        settle_ticks=20_000,
        capture_ticks=100_000,
        pulse_ticks=100_000,
        lead_ticks=500_000,
    )


def test_session_runs_every_edge_keeps_queue_bounded_and_stages_one_hop_ahead():
    dev = FakeTickDevice()
    p = plan(6)
    pipeline = FhmPipelineModel(init_hz=p.init_frequency_hz)
    stager = FakeStager(pipeline)

    def on_edge(executed, expected):  # the fake PLL follows the model exactly
        stager.pll = expected

    s = TickSession(dev.read_attr, dev.write_attr, lookahead_hops=3, sleep=lambda _s: None)
    compiled = compile_plan(p, t0_ticks=dev.now, tick_hz=TICK_HZ)
    results = s.run(compiled, stager, pipeline, after_edge=on_edge)
    assert len(results) == len(compiled.edges) == 7
    assert all(r.status == "EXECUTED" and r.executed_ticks == r.requested_ticks for r in results)
    assert [r.readback_hz for r in results] == [e.commits_hz for e in compiled.edges]
    assert stager.staged == list(p.frequencies_hz), "frequencies[k] staged before edge k, once each"
    assert len(dev.pushed) == 2 * len(compiled.edges)
    # 2 descriptors per hop in flight plus the completed hop's not-yet-due falling edge.
    assert dev.max_queue_level == 2 * 3 + 1


def test_lookahead_is_clamped_so_a_16_deep_queue_never_overflows():
    dev = FakeTickDevice()
    p = plan(12)
    pipeline = FhmPipelineModel(init_hz=p.init_frequency_hz)
    stager = FakeStager(pipeline)
    s = TickSession(dev.read_attr, dev.write_attr, lookahead_hops=99, sleep=lambda _s: None)
    compiled = compile_plan(p, t0_ticks=dev.now, tick_hz=TICK_HZ)
    results = s.run(
        compiled, stager, pipeline, after_edge=lambda ex, hz: setattr(stager, "pll", hz)
    )
    assert all(r.status == "EXECUTED" for r in results)
    assert dev.max_queue_level <= 16 and dev.model.overflow_count == 0
    assert dev.model.queue_level == 0 and dev.model.gpio_out & 1 == 0, (
        "run ends with trigger low, queue empty"
    )
    assert dev.model.late_count == 0 and dev.model.overflow_count == 0
    assert dev.model.queue_level == 0
    assert not any(r.stage_late for r in results)


def test_session_reports_late_dropped_edge_without_advancing_pipeline():
    dev = FakeTickDevice(step=50_000)
    p = plan(3)
    pipeline = FhmPipelineModel(init_hz=p.init_frequency_hz)
    stager = FakeStager(pipeline)
    s = TickSession(dev.read_attr, dev.write_attr, lookahead_hops=2, sleep=lambda _s: None)
    # The clock is already past edge 0 when the plan is submitted -> LATE_DROPPED.
    dev.now = p.lead_ticks + 10
    dev.model.advance_to(dev.now)
    compiled = compile_plan(p, t0_ticks=0, tick_hz=TICK_HZ)
    results = s.run(
        compiled, stager, pipeline, after_edge=lambda ex, hz: setattr(stager, "pll", hz)
    )
    assert results[0].status == "LATE_DROPPED" and results[0].executed_ticks is None
    assert results[1].status == "EXECUTED"
    # dropped priming edge: edge 1 now commits init (pipeline did not move), and the
    # session reports what the PLL *should* read, not the plan's wish.
    assert results[1].readback_hz == p.init_frequency_hz


def test_session_requires_gpio_capability_and_tick_hz():
    dev = FakeTickDevice()
    s = TickSession(dev.read_attr, dev.write_attr, sleep=lambda _s: None)
    info = s.probe()
    assert info["time_tick_hz"] == TICK_HZ and info["queue_depth"] == 16 and info["gpio_capable"]
