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
from dataclasses import asdict, dataclass

import numpy as np

from hopcap import vrt
from hopcap.timebase import beats_from_fraction


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
    """IF data samples with their Tick-time stamps (one RX channel, complex)."""

    ticks: np.ndarray  # float64 Tick time of each sample
    iq: np.ndarray  # complex128

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
        t_parts: list[np.ndarray] = []
        z_parts: list[np.ndarray] = []
        ticks_per_beat = tick_hz / beats_per_pps
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
            k0 = beats_from_fraction(p.frac_ps, ps_per_sample_q32)
            t0 = p.int_secs * tick_hz + k0 * ticks_per_beat
            t_parts.append(t0 + np.arange(len(iq)) * ticks_per_beat)
            z_parts.append(iq[:, 0].astype(np.float64) + 1j * iq[:, 1].astype(np.float64))
        if not t_parts:
            return cls(np.zeros(0), np.zeros(0, dtype=complex))
        ticks = np.concatenate(t_parts)
        iq = np.concatenate(z_parts)
        order = np.argsort(ticks, kind="stable")
        return cls(ticks[order], iq[order])

    def window(self, start: float, stop: float) -> tuple[np.ndarray, np.ndarray]:
        lo, hi = np.searchsorted(self.ticks, [start, stop])
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
        return HopTiming(**base, crossing_ticks=None, latency_ns=None, old_tone_db=None,
                         new_tone_db=None, note="no samples around the edge")
    t_s = (t - executed_ticks) / tick_hz
    p_old = _tone_power(t_s, z, f_old, window)
    p_new = _tone_power(t_s, z, f_new, window)
    half = window // 2
    valid = slice(half, len(z) - half)
    # strictly before the edge by more than half the averaging window
    pre = (t_s < -half * float(np.median(np.diff(t_s))) if len(t_s) > 1 else t_s < 0) & (
        np.arange(len(z)) >= half
    )
    if not pre.any():
        return HopTiming(**base, crossing_ticks=None, latency_ns=None, old_tone_db=None,
                         new_tone_db=None, note="no samples before the edge")
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
        return HopTiming(**base, crossing_ticks=None, latency_ns=None, old_tone_db=old_db,
                         new_tone_db=None, note="new tone never dominated")
    # sub-sample: linear interpolation of (p_new - p_old) through zero
    if crossing > 0:
        d0 = p_new[crossing - 1] - p_old[crossing - 1]
        d1 = p_new[crossing] - p_old[crossing]
        frac = d0 / (d0 - d1) if d1 != d0 else 0.0
        t_cross = t[crossing - 1] + frac * (t[crossing] - t[crossing - 1])
    else:
        t_cross = t[crossing]
    after = idx >= crossing + window
    new_db = (
        float(10 * np.log10(np.median(p_new[after]) / max(np.median(p_old[after]), 1e-12)))
        if after.any()
        else None
    )
    return HopTiming(
        **base,
        crossing_ticks=float(t_cross),
        latency_ns=float((t_cross - executed_ticks) / tick_hz * 1e9),
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
    out = {"hops": len(hops), "measured": len(measured), "all": stats([h.latency_ns for h in measured])}
    for kind in ("rx-only", "tx-only", "both"):
        out[kind] = stats([h.latency_ns for h in measured if h.kind == kind])
    return out
