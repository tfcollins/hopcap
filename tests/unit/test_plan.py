# SPDX-License-Identifier: MIT
"""Hop plan compilation: slot table, FHM one-behind pipeline, edge timing, fail-closed checks."""

from __future__ import annotations

import pytest

from hopcap.plan import (
    FhmPipelineModel,
    HopPlan,
    PlanError,
    SlotTable,
    compile_plan,
    hz_to_q44_20,
)

TICK_HZ = 100_000_000
MHZ = 1_000_000
INIT = 2_450 * MHZ
A, B, C = 2_410 * MHZ, 2_420 * MHZ, 2_430 * MHZ


def plan(**kw) -> HopPlan:
    base = dict(
        frequencies_hz=(A, B, C),
        period_ticks=5_000_000,
        settle_ticks=20_000,
        capture_ticks=1_000_000,
        pulse_ticks=1_000_000,
        lead_ticks=5_000_000,
    )
    base.update(kw)
    return HopPlan(**base)


def test_q44_20_is_exact_for_integer_hz():
    assert hz_to_q44_20(2_450_000_000) == 2_450_000_000 << 20
    assert hz_to_q44_20(1) == 1 << 20


def test_slot_table_always_contains_init_frequency_first():
    t = SlotTable.from_plan(plan())
    assert t.slot_for(INIT) == 0
    assert {t.slot_for(A), t.slot_for(B), t.slot_for(C)} == {1, 2, 3}
    assert t.words()[0] == hz_to_q44_20(INIT)


def test_slot_table_dedups_repeated_frequencies():
    t = SlotTable.from_plan(plan(frequencies_hz=(A, B, A, C, A)))
    assert len(t) == 4


def test_slot_table_rejects_more_than_eight_unique():
    freqs = tuple(2_401 * MHZ + i * MHZ for i in range(8))  # 8 + init = 9
    with pytest.raises(PlanError, match="slot"):
        SlotTable.from_plan(plan(frequencies_hz=freqs))


def test_edges_follow_the_one_behind_pipeline():
    """Edge k commits the frequency staged before edge k-1; edge 0 is the priming edge
    that lands on fhmInitFrequency; frequencies[k] is staged before edge k fires."""
    c = compile_plan(plan(), t0_ticks=1_000, tick_hz=TICK_HZ)
    assert [e.commits_hz for e in c.edges] == [INIT, A, B, C]
    assert [e.stage_before_hz for e in c.edges] == [A, B, C, None]
    assert c.edges[0].priming is True
    assert all(e.priming is False for e in c.edges[1:])
    assert [e.slot for e in c.edges] == [c.slots.slot_for(f) for f in (INIT, A, B, C)]


def test_edge_timing_is_lead_plus_k_period_with_pulse_width():
    p = plan()
    c = compile_plan(p, t0_ticks=1_000, tick_hz=TICK_HZ)
    for k, e in enumerate(c.edges):
        assert e.rise_ticks == 1_000 + p.lead_ticks + k * p.period_ticks
        assert e.fall_ticks == e.rise_ticks + p.pulse_ticks


def test_period_must_cover_settle_capture_and_pulse():
    with pytest.raises(PlanError, match="period"):
        compile_plan(plan(period_ticks=1_000_000 + 20_000 + 1_000_000), t0_ticks=0, tick_hz=TICK_HZ)


def test_out_of_band_frequency_rejected():
    with pytest.raises(PlanError, match="band"):
        compile_plan(plan(frequencies_hz=(2_600 * MHZ,)), t0_ticks=0, tick_hz=TICK_HZ)


def test_pipeline_model_tracks_executed_and_dropped_edges():
    """Mirrors the validated tron sequence: arm(A), edge -> init; arm(B), edge -> A; ..."""
    m = FhmPipelineModel(init_hz=INIT)
    m.stage(A)
    assert m.on_edge(executed=True) == INIT  # priming edge lands on init
    m.stage(B)
    assert m.on_edge(executed=True) == A
    m.stage(C)
    assert m.on_edge(executed=False) == A  # LATE_DROPPED: nothing moves
    assert m.on_edge(executed=True) == B
    assert m.expected_hz == B
    assert m.on_edge(executed=True) == C  # drain edge with nothing new staged
    assert m.on_edge(executed=True) == C  # ... and the pipeline holds steady


def test_capture_samples_round_trip_ticks():
    p = plan()
    assert p.capture_samples(sample_rate_hz=245_760_000) == 1_000_000 * 245_760_000 // TICK_HZ
    assert (
        HopPlan.ticks_for_samples(2_457_600, sample_rate_hz=245_760_000, tick_hz=TICK_HZ)
        == 1_000_000
    )
