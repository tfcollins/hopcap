# SPDX-License-Identifier: MIT
"""Spool -> per-hop CaptureRecords joined with Context and Tick edge results."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synth import stream_for_plan

from hopcap.associate import EdgeResult, associate
from hopcap.plan import HopPlan, compile_plan

TICK_HZ = 100_000_000
FS = 245_760_000
MHZ = 1_000_000
SPP = 360


def compiled():
    p = HopPlan(
        frequencies_hz=(2_410 * MHZ, 2_420 * MHZ, 2_430 * MHZ),
        period_ticks=200_000,
        settle_ticks=5_000,
        capture_ticks=10_000,
        pulse_ticks=1_000,
        lead_ticks=10_000,
        context_margin_ticks=1_000,
    )
    return compile_plan(p, t0_ticks=0, tick_hz=TICK_HZ)


def edge_results(c, *, dropped: set[int] = frozenset()):
    out = []
    for e in c.edges:
        executed = e.index not in dropped
        out.append(
            EdgeResult(
                index=e.index,
                slot=e.slot,
                commits_hz=e.commits_hz,
                requested_ticks=e.rise_ticks,
                executed_ticks=e.rise_ticks if executed else None,
                status="EXECUTED" if executed else "LATE_DROPPED",
                readback_hz=e.commits_hz if executed else None,
                stage_late=False,
            )
        )
    return out


def test_clean_stream_yields_one_complete_tagged_capture_per_edge():
    c = compiled()
    recs = stream_for_plan(c, fs=FS, samples_per_packet=SPP)
    res = associate(
        recs, c, edge_results(c), sample_rate_hz=FS, samples_per_packet=SPP, epoch_base=0
    )
    assert res.counts.malformed == 0 and res.counts.foreign_peer == 0
    assert res.counts.context == 4 and res.counts.data > 0
    assert len(res.hops) == 4
    expect_samples = c.plan.capture_samples(sample_rate_hz=FS, tick_hz=TICK_HZ)
    for e, h in zip(c.edges, res.hops, strict=True):
        assert h.epoch == e.index + 1
        assert h.context_hz == e.commits_hz == h.planned_hz == h.readback_hz
        assert h.samples == expect_samples
        assert h.packets == -(-expect_samples // SPP)
        assert h.gaps == 0 and h.complete and h.disposition == "ok"
        assert h.priming is (e.index == 0)
        assert (
            h.first_sample_ps == (e.rise_ticks + c.plan.settle_ticks) * FS // TICK_HZ * 10**12 // FS
        )
    assert res.disposition == "ok"


def test_lost_context_marks_that_hop_invalid_without_relabelling_neighbours():
    c = compiled()
    recs = stream_for_plan(c, fs=FS, samples_per_packet=SPP, drop_context_for={2})
    res = associate(
        recs, c, edge_results(c), sample_rate_hz=FS, samples_per_packet=SPP, epoch_base=0
    )
    assert len(res.hops) == 4
    bad = res.hops[2]
    assert bad.context_hz is None and bad.epoch is None
    assert bad.disposition == "invalid" and "no_context" in bad.reasons
    assert bad.samples > 0, "its data window is still accounted for"
    assert res.hops[3].epoch == 4 and res.hops[3].context_hz == c.edges[3].commits_hz
    assert res.hops[1].disposition == "ok"
    assert res.counts.epoch_skips == 1


def test_lost_data_packets_make_the_hop_incomplete_but_still_tagged():
    c = compiled()
    recs = stream_for_plan(c, fs=FS, samples_per_packet=SPP, drop_data_packets={1: {0, 3}})
    res = associate(
        recs, c, edge_results(c), sample_rate_hz=FS, samples_per_packet=SPP, epoch_base=0
    )
    h = res.hops[1]
    assert h.epoch == 2 and h.context_hz == c.edges[1].commits_hz
    assert not h.complete and h.disposition == "incomplete"
    assert h.gaps == 2 and h.lost_packets_estimate == 2
    assert res.hops[0].complete and res.hops[2].complete


def test_dropped_edge_has_no_context_and_no_data_and_is_not_matched_to_the_next():
    """Tick LATE_DROPPED edge 1: the FPGA never saw it, so the wire carries epochs 1,2,3 for
    edges 0,2,3.  Edge 1 must come out invalid and must not steal edge 2's Context."""
    from dataclasses import replace

    c = compiled()
    keep = [e for e in c.edges if e.index != 1]
    on_wire = replace(c, edges=tuple(replace(e, index=i) for i, e in enumerate(keep)))
    recs = stream_for_plan(on_wire, fs=FS, samples_per_packet=SPP)
    res = associate(
        recs,
        c,
        edge_results(c, dropped={1}),
        sample_rate_hz=FS,
        samples_per_packet=SPP,
        epoch_base=0,
    )
    assert len(res.hops) == 4
    assert res.hops[1].disposition == "invalid" and "edge_not_executed" in res.hops[1].reasons
    assert res.hops[1].epoch is None and res.hops[1].samples == 0
    assert [h.epoch for h in res.hops if h.epoch is not None] == [1, 2, 3]
    assert res.hops[2].context_hz == c.edges[2].commits_hz and res.hops[2].disposition == "ok"
    assert res.hops[3].context_hz == c.edges[3].commits_hz and res.hops[3].disposition == "ok"
    assert res.disposition == "invalid"


def test_foreign_peer_and_malformed_are_counted_not_parsed():
    from hopcap.spool import Record

    c = compiled()
    recs = stream_for_plan(c, fs=FS, samples_per_packet=SPP)
    recs.insert(1, Record(mono_ns=0, real_ns=0, peer_ip="10.9.9.9", peer_port=5, data=recs[0].data))
    recs.insert(
        2, Record(mono_ns=0, real_ns=0, peer_ip="192.0.2.1", peer_port=4991, data=b"\x00" * 9)
    )
    res = associate(
        recs,
        c,
        edge_results(c),
        sample_rate_hz=FS,
        samples_per_packet=SPP,
        epoch_base=0,
        expected_peer=("192.0.2.1", 4991),
    )
    assert res.counts.foreign_peer == 1 and res.counts.malformed == 1
    assert res.disposition == "ok"
