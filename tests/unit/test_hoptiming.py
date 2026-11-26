# SPDX-License-Identifier: MIT
"""hopcap.hoptiming: hop landing time from a synthetic loopback tone."""

from __future__ import annotations

import numpy as np
import pytest

from hopcap.hoptiming import SampleStream, hop_kind, measure_hop, summarize

FS = 15.36e6
TICK = 50_000_000
F_DDS = 1_000_000
# (rx_lo, tx_lo): tone = tx - rx + f_dds
SLOTS = [(0, 0), (0, 2_000_000), (3_000_000, 2_000_000), (1_000_000, 4_000_000)]


def _stream(f_before: float, f_after: float, *, latency_s: float, n_ms: float = 2.0, seed=0):
    rng = np.random.default_rng(seed)
    n = int(FS * n_ms * 1e-3)
    t = np.arange(n) / FS
    edge = n // 2
    f = np.where(t < t[edge] + latency_s, f_before, f_after)
    z = np.exp(1j * 2 * np.pi * np.cumsum(f) / FS) * 1000
    z += (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 30
    return SampleStream(t * TICK, z), int(t[edge] * TICK)


def test_hop_kind():
    assert hop_kind(SLOTS[0], SLOTS[1]) == "tx-only"
    assert hop_kind(SLOTS[1], SLOTS[2]) == "rx-only"
    assert hop_kind(SLOTS[2], SLOTS[3]) == "both"


@pytest.mark.parametrize(("a", "b", "kind"), [(0, 1, "tx-only"), (1, 2, "rx-only"), (2, 3, "both")])
@pytest.mark.parametrize("latency_us", [0.0, 3.0, 17.5])
def test_measures_latency_to_within_one_sample(a, b, kind, latency_us):
    f = [s[1] - s[0] + F_DDS for s in SLOTS]
    st, edge = _stream(f[a], f[b], latency_s=latency_us * 1e-6)
    h = measure_hop(
        st,
        index=0,
        executed_ticks=edge,
        from_slot=a,
        to_slot=b,
        slots=SLOTS,
        f_dds_hz=F_DDS,
        tick_hz=TICK,
    )
    assert h.kind == kind
    assert h.latency_ns == pytest.approx(latency_us * 1e3, abs=65.1)  # one sample at 15.36 MSPS
    assert h.old_tone_db > 20 and h.new_tone_db > 20


def test_a_hop_that_never_happens_is_not_measured():
    f0 = SLOTS[0][1] - SLOTS[0][0] + F_DDS
    st, edge = _stream(f0, f0, latency_s=0)
    h = measure_hop(
        st,
        index=0,
        executed_ticks=edge,
        from_slot=0,
        to_slot=1,
        slots=SLOTS,
        f_dds_hz=F_DDS,
        tick_hz=TICK,
    )
    assert h.latency_ns is None and h.note == "new tone never dominated"


def test_an_early_hop_shows_as_negative_latency():
    f = [s[1] - s[0] + F_DDS for s in SLOTS]
    st, edge = _stream(f[0], f[1], latency_s=-50e-6)
    h = measure_hop(
        st,
        index=0,
        executed_ticks=edge,
        from_slot=0,
        to_slot=1,
        slots=SLOTS,
        f_dds_hz=F_DDS,
        tick_hz=TICK,
        pre_s=200e-6,
    )
    assert h.latency_ns == pytest.approx(-50_000, abs=50)


def test_no_samples_around_the_edge():
    st = SampleStream(np.zeros(0), np.zeros(0, dtype=complex))
    h = measure_hop(
        st,
        index=3,
        executed_ticks=10**9,
        from_slot=0,
        to_slot=1,
        slots=SLOTS,
        f_dds_hz=F_DDS,
        tick_hz=TICK,
    )
    assert h.latency_ns is None and h.note == "no samples around the edge"


def test_summarize_per_kind():
    f = [s[1] - s[0] + F_DDS for s in SLOTS]
    hops = []
    for i, (a, b) in enumerate([(0, 1), (1, 2), (2, 3), (3, 0)]):
        st, edge = _stream(f[a], f[b], latency_s=5e-6, seed=i)
        hops.append(
            measure_hop(
                st,
                index=i,
                executed_ticks=edge,
                from_slot=a,
                to_slot=b,
                slots=SLOTS,
                f_dds_hz=F_DDS,
                tick_hz=TICK,
            )
        )
    s = summarize(hops)
    assert s["measured"] == 4 and s["all"]["n"] == 4
    assert s["tx-only"]["n"] == 1 and s["rx-only"]["n"] == 1 and s["both"]["n"] == 2
    assert s["all"]["spread_ns"] < 65.1


def test_borrow_stamps_decode_to_before_the_pps_edge():
    from hopcap.hoptiming import stamp_candidates

    q = 279620266666667  # 15.36 MSPS
    tpb = TICK / 15_360_000
    m = 7  # first sample 7 beats before the edge of second 100
    frac = 10**12 - ((m * q) >> 32)
    c = stamp_candidates(99, frac, tick_hz=TICK, ps_per_sample_q32=q, ticks_per_beat=tpb)
    assert (100 * TICK, -m * tpb) in c
    k = 1234
    c = stamp_candidates(100, (k * q) >> 32, tick_hz=TICK, ps_per_sample_q32=q, ticks_per_beat=tpb)
    assert c[0] == (100 * TICK, k * tpb)


def test_absolute_tick_times_keep_sub_tick_resolution():
    # Real Tick times are ~9e16: a float64 there resolves only 16 ticks. The stream keeps
    # an integer base, so a hop still measures to within a sample.
    f = [s[1] - s[0] + F_DDS for s in SLOTS]
    st, edge = _stream(f[0], f[1], latency_s=4e-6)
    base = 89_783_069_639_043_217
    st = SampleStream(st.ticks, st.iq, base_ticks=base)
    h = measure_hop(
        st,
        index=0,
        executed_ticks=base + edge,
        from_slot=0,
        to_slot=1,
        slots=SLOTS,
        f_dds_hz=F_DDS,
        tick_hz=TICK,
    )
    assert h.latency_ns == pytest.approx(4000, abs=65.1)
