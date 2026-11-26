# SPDX-License-Identifier: MIT
"""Where each ADRV9361 hop lands, measured from the vrt49 capture (gapless runs).

With the TX DDS tone looped back into RX, slot k puts the RX baseband tone at
``tx_lo_k - rx_lo_k + f_dds``. For every executed Tick edge from slot a to slot b this module
takes the samples around the edge from the datagram spool, converts each sample's VRT stamp
to Tick time with the *measured* samples-per-PPS (:func:`hopcap.timebase.vrt_to_tick_ticks`),
and finds the first sample where the tone at the new offset carries more power than the tone
at the old one (both demodulated and averaged over ``window`` samples). The hop's latency is
that crossing minus the Tick EXECUTED timestamp.

A hop is classified as RX-only (only the RX LO changed), TX-only (only the TX LO changed) or
both; the latency of a fixed pin-to-RF path should be the same constant for every hop of a
class, and the classes should agree closely, because both directions are switched by the same
three CTRL_IN pins on the same Tick edge.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field

import numpy as np

from hopcap import vrt
from hopcap.timebase import TimeBaseError, beats_from_fraction

PS_PER_SEC = 10**12


def stamp_candidates(
    sec: int, frac_ps: int, *, tick_hz: int, ps_per_sample_q32: int, ticks_per_beat: float
) -> list[float]:
    """Tick times a framer stamp can stand for, as ``(integer second edge, offset)`` pairs.

    Absolute Tick times (~1e17) do not fit a float64 to the tick, so the edge stays an int.

    The framer stamps a packet ``k`` beats after the tuple's PPS edge as
    ``(sec, floor(k * q / 2^32))``; a packet whose first sample predates that edge by ``m``
    beats (vrt49 axi_vrt49_framer "borrow" path) as ``(sec - 1, 1e12 - floor(m * q / 2^32))``.
    Values on both grids return both readings; the caller disambiguates by continuity."""
    out = []
    try:
        out.append(
            (sec * tick_hz, beats_from_fraction(frac_ps, ps_per_sample_q32) * ticks_per_beat)
        )
    except TimeBaseError:
        pass
    try:
        m = beats_from_fraction(PS_PER_SEC - frac_ps, ps_per_sample_q32)
        out.append(((sec + 1) * tick_hz, -m * ticks_per_beat))
    except TimeBaseError:
        pass
    return out


@dataclass(frozen=True)
class HopTiming:
    index: int
    from_slot: int
    to_slot: int
    kind: str  # "rx-only" | "tx-only" | "both"
    executed_ticks: int
    crossing_ticks: float | None
    latency_ns: float | None
    old_tone_db: float | None  # power ratio old/new before the edge (proves no early hop)
    new_tone_db: float | None  # power ratio new/old after the crossing
    samples: int
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def hop_kind(a: tuple[int, int], b: tuple[int, int]) -> str:
    """(rx_lo, tx_lo) before and after: which synthesizers the hop moved."""
    rx, tx = a[0] != b[0], a[1] != b[1]
    if rx and tx:
        return "both"
    if rx:
        return "rx-only"
    if tx:
        return "tx-only"
    return "none"


@dataclass
class SampleStream:
    """IF data samples with their Tick-time stamps (one RX channel, complex).

    ``ticks`` is relative to the integer ``base_ticks``: absolute Tick times (~1e17) lose
    16 ticks of resolution in a float64."""

    ticks: np.ndarray  # float64 Tick time of each sample minus base_ticks
    iq: np.ndarray  # complex128
    base_ticks: int = 0
    periods: dict = field(default_factory=dict)  # second -> measured beats

    @classmethod
    def from_records(
        cls,
        records: Iterable,
        *,
        tick_hz: int,
        ps_per_sample_q32: int,
        beats_per_pps: int,
        expected_peer: tuple[str, int] | None = None,
    ) -> SampleStream:
        """Map every sample to Tick time using each second's *measured* length.

        The framer stamps with the nominal sample period, but the AD9361 sample clock and
        the Tick clock (which makes the PPS) are independent oscillators, so a second holds
        a few tens of samples more or fewer than nominal and it varies second to second. In
        a continuous stream every PPS crossing is visible: second S's length is the beat
        count from its first sample to the next second's first sample. A sample k beats
        after edge S is at Tick ``S * tick_hz + k * tick_hz / P_S``; ``beats_per_pps`` is
        only the fallback for a second whose length the stream does not show.
        """
        nominal = ps_per_sample_q32 / 2**32 / PS_PER_SEC  # seconds per beat (stamp grid)
        pkts: list[tuple[int, int, np.ndarray]] = []  # (edge second S, beats k after it, iq)
        expect: float | None = None  # seconds since the first edge, nominal grid
        s0: int | None = None
        for r in records:
            if expected_peer and (r.peer_ip, r.peer_port) != expected_peer:
                continue
            try:
                p = vrt.parse(r.data)
            except vrt.VrtError:
                continue
            if not isinstance(p, vrt.IfData):
                continue
            iq = p.iq()
            if not len(iq):
                continue
            cands = []
            try:
                cands.append((p.int_secs, beats_from_fraction(p.frac_ps, ps_per_sample_q32)))
            except TimeBaseError:
                pass
            try:
                m = beats_from_fraction(PS_PER_SEC - p.frac_ps, ps_per_sample_q32)
                cands.append((p.int_secs + 1, -m))
            except TimeBaseError:
                pass
            if not cands:
                raise TimeBaseError(
                    f"stamp ({p.int_secs}, {p.frac_ps} ps) is on neither framer grid for "
                    f"ps_per_sample_q32 {ps_per_sample_q32}"
                )
            if s0 is None:
                s0 = cands[0][0]
            pos = [(S - s0) + k * nominal for S, k in cands]
            pick = (
                0
                if expect is None or len(cands) == 1
                else min(range(len(cands)), key=lambda c: abs(pos[c] - expect))
            )
            S, k = cands[pick]
            pkts.append((S, k, iq))
            expect = pos[pick] + len(iq) * nominal
        if not pkts:
            return cls(np.zeros(0), np.zeros(0, dtype=complex))
        # second lengths from consecutive packets across each PPS crossing
        periods: dict[int, int] = {}
        for (sa, ka, za), (sb, kb, _) in zip(pkts, pkts[1:]):
            if sb == sa + 1:
                periods[sa] = ka + len(za) - kb
        t_parts, z_parts = [], []
        for S, k, iq in pkts:
            sec = S - 1 if k < 0 else S  # borrow: k beats before edge S, inside second S-1
            per = periods.get(sec, beats_per_pps)
            tpb = tick_hz / per
            t0 = (S - s0) * tick_hz + k * tpb
            t_parts.append(t0 + np.arange(len(iq)) * tpb)
            z_parts.append(iq[:, 0].astype(np.float64) + 1j * iq[:, 1].astype(np.float64))
        ticks = np.concatenate(t_parts)
        iq = np.concatenate(z_parts)
        order = np.argsort(ticks, kind="stable")
        out = cls(ticks[order], iq[order], base_ticks=int(s0) * tick_hz)
        out.periods = periods
        return out

    def window(self, start: int | float, stop: int | float) -> tuple[np.ndarray, np.ndarray]:
        """Samples with absolute Tick time in [start, stop); times returned relative to base."""
        lo, hi = np.searchsorted(self.ticks, [start - self.base_ticks, stop - self.base_ticks])
        return self.ticks[lo:hi], self.iq[lo:hi]


def _tone_power(t_s: np.ndarray, z: np.ndarray, f_hz: float, window: int) -> np.ndarray:
    d = z * np.exp(-2j * np.pi * f_hz * t_s)
    k = np.ones(window) / window
    return np.abs(np.convolve(d, k, mode="same")) ** 2


def measure_hop(
    stream: SampleStream,
    *,
    index: int,
    executed_ticks: int,
    from_slot: int,
    to_slot: int,
    slots: Sequence[tuple[int, int]],
    f_dds_hz: float,
    tick_hz: int,
    pre_s: float = 200e-6,
    post_s: float = 400e-6,
    window: int = 64,
) -> HopTiming:
    a, b = slots[from_slot], slots[to_slot]
    kind = hop_kind(a, b)
    f_old = a[1] - a[0] + f_dds_hz
    f_new = b[1] - b[0] + f_dds_hz
    t, z = stream.window(executed_ticks - pre_s * tick_hz, executed_ticks + post_s * tick_hz)
    base = dict(
        index=index,
        from_slot=from_slot,
        to_slot=to_slot,
        kind=kind,
        executed_ticks=executed_ticks,
        samples=len(z),
    )
    if len(z) < 4 * window:
        return HopTiming(
            **base,
            crossing_ticks=None,
            latency_ns=None,
            old_tone_db=None,
            new_tone_db=None,
            note="no samples around the edge",
        )
    t_s = (t - (executed_ticks - stream.base_ticks)) / tick_hz
    p_old = _tone_power(t_s, z, f_old, window)
    p_new = _tone_power(t_s, z, f_new, window)
    half = window // 2
    valid = slice(half, len(z) - half)
    # strictly before the edge by more than half the averaging window
    pre = (t_s < -half * float(np.median(np.diff(t_s))) if len(t_s) > 1 else t_s < 0) & (
        np.arange(len(z)) >= half
    )
    if not pre.any():
        return HopTiming(
            **base,
            crossing_ticks=None,
            latency_ns=None,
            old_tone_db=None,
            new_tone_db=None,
            note="no samples before the edge",
        )
    old_db = float(10 * np.log10(np.median(p_old[pre]) / max(np.median(p_new[pre]), 1e-12)))
    newer = (p_new > p_old) & (t_s > -1e-3)
    idx = np.arange(len(z))
    cand = idx[valid][newer[valid]]
    # the crossing must be followed by the new tone holding: require the next `window`
    # samples to stay new-dominant
    crossing = None
    for i in cand:
        j = min(len(z) - half, i + window)
        if np.all(p_new[i:j] > p_old[i:j]):
            crossing = int(i)
            break
    if crossing is None:
        return HopTiming(
            **base,
            crossing_ticks=None,
            latency_ns=None,
            old_tone_db=old_db,
            new_tone_db=None,
            note="new tone never dominated",
        )
    # sub-sample: linear interpolation of (p_new - p_old) through zero
    if crossing > 0:
        d0 = p_new[crossing - 1] - p_old[crossing - 1]
        d1 = p_new[crossing] - p_old[crossing]
        frac = d0 / (d0 - d1) if d1 != d0 else 0.0
        t_cross = t[crossing - 1] + frac * (t[crossing] - t[crossing - 1])
    else:
        t_cross = t[crossing]
    rel_cross = float(t_cross - (executed_ticks - stream.base_ticks))  # ticks after the edge
    after = idx >= crossing + window
    new_db = (
        float(10 * np.log10(np.median(p_new[after]) / max(np.median(p_old[after]), 1e-12)))
        if after.any()
        else None
    )
    return HopTiming(
        **base,
        crossing_ticks=executed_ticks + rel_cross,
        latency_ns=rel_cross / tick_hz * 1e9,
        old_tone_db=old_db,
        new_tone_db=new_db,
    )


def summarize(hops: Sequence[HopTiming]) -> dict:
    """Latency statistics overall and per hop kind (ns)."""

    def stats(vals: list[float]) -> dict:
        if not vals:
            return {"n": 0}
        return {
            "n": len(vals),
            "mean_ns": statistics.fmean(vals),
            "min_ns": min(vals),
            "max_ns": max(vals),
            "spread_ns": max(vals) - min(vals),
            "stdev_ns": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
        }

    measured = [h for h in hops if h.latency_ns is not None]
    out = {
        "hops": len(hops),
        "measured": len(measured),
        "all": stats([h.latency_ns for h in measured]),
    }
    for kind in ("rx-only", "tx-only", "both"):
        out[kind] = stats([h.latency_ns for h in measured if h.kind == kind])
    return out
