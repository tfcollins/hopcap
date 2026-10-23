# SPDX-License-Identifier: MIT
"""SigMF + per-hop npy + manifest export from an AssociationResult."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synth import stream_for_plan

from hopcap.associate import EdgeResult, associate
from hopcap.export import MANIFEST_SCHEMA, write_bundle
from hopcap.plan import HopPlan, compile_plan

TICK_HZ, FS, SPP, MHZ = 100_000_000, 245_760_000, 360, 1_000_000


def _run(tmp_path, drop_context_for=frozenset()):
    p = HopPlan(
        frequencies_hz=(2_410 * MHZ, 2_420 * MHZ),
        period_ticks=200_000,
        settle_ticks=5_000,
        capture_ticks=10_000,
        pulse_ticks=1_000,
        lead_ticks=10_000,
        context_margin_ticks=1_000,
    )
    c = compile_plan(p, t0_ticks=0, tick_hz=TICK_HZ)
    recs = stream_for_plan(c, fs=FS, samples_per_packet=SPP, drop_context_for=drop_context_for)
    edges = [
        EdgeResult(
            index=e.index,
            slot=e.slot,
            commits_hz=e.commits_hz,
            requested_ticks=e.rise_ticks,
            executed_ticks=e.rise_ticks,
            status="EXECUTED",
            readback_hz=e.commits_hz,
            stage_late=False,
        )
        for e in c.edges
    ]
    res = associate(recs, c, edges, sample_rate_hz=FS, samples_per_packet=SPP, epoch_base=0)
    out = write_bundle(
        tmp_path / "out",
        compiled=c,
        result=res,
        records=recs,
        sample_rate_hz=FS,
        provenance={"hopcap_commit": "abc", "tick_commit": "def"},
        counters={"framer_tx_dropped_delta": 0},
    )
    return c, res, out


def test_sigmf_has_one_capture_segment_per_complete_hop_with_its_frequency(tmp_path):
    c, res, out = _run(tmp_path)
    meta = json.loads((out / "run.sigmf-meta").read_text())
    assert meta["global"]["core:datatype"] == "ci16_le"
    assert meta["global"]["core:sample_rate"] == FS
    caps = meta["captures"]
    assert len(caps) == len(c.edges)
    assert [cap["core:frequency"] for cap in caps] == [e.commits_hz for e in c.edges]
    starts = [cap["core:sample_start"] for cap in caps]
    assert starts == sorted(starts) and starts[0] == 0
    data = np.fromfile(out / "run.sigmf-data", dtype="<i2").reshape(-1, 2)
    assert len(data) == sum(h.samples for h in res.hops)
    for cap, h in zip(caps, res.hops, strict=True):
        ann = next(
            a for a in meta["annotations"] if a["core:sample_start"] == cap["core:sample_start"]
        )
        assert ann["hopcap:epoch"] == h.epoch and ann["core:sample_count"] == h.samples


def test_invalid_hops_are_excluded_from_sigmf_but_present_in_manifest(tmp_path):
    c, _res, out = _run(tmp_path, drop_context_for={1})
    meta = json.loads((out / "run.sigmf-meta").read_text())
    assert len(meta["captures"]) == len(c.edges) - 1
    man = json.loads((out / "manifest.json").read_text())
    assert man["schema"] == MANIFEST_SCHEMA
    assert len(man["hops"]) == len(c.edges)
    assert man["hops"][1]["disposition"] == "invalid"
    assert man["disposition"] == "invalid"
    assert man["clock_domains"]["tick"]["mapped_to_vrt"] is False


def test_per_hop_npy_and_manifest_provenance(tmp_path):
    c, res, out = _run(tmp_path)
    for h in res.hops:
        arr = np.load(out / "hops" / f"hop_{h.index:04d}.npy")
        assert arr.dtype == np.int16 and arr.shape == (h.samples, 2)
    man = json.loads((out / "manifest.json").read_text())
    assert man["provenance"]["tick_commit"] == "def"
    assert man["counters"]["framer_tx_dropped_delta"] == 0
    assert man["plan"]["frequencies_hz"] == list(c.plan.frequencies_hz)
    assert (out / "datagrams.spool").stat().st_size > 0
    assert set(json.loads((out / "SHA256SUMS.json").read_text())) >= {
        "run.sigmf-data",
        "manifest.json",
        "datagrams.spool",
    }
