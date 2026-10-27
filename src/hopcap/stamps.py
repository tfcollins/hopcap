# SPDX-License-Identifier: MIT
"""Check the framer's VRT timestamps against the sample count.

Inside one capture the framer stamps each packet ``floor(k * q / 2**32)`` ps after the last
PPS edge, where ``k`` counts samples since that edge and ``q`` is ``v49_tb_ps_per_sample_q32``.
Consecutive packets ``n`` samples apart therefore differ by ``floor(n * q / 2**32)`` or one ps
more, unless a PPS edge falls between them: the time base re-anchors there, so that step also
carries the PPS period's offset from nominal and is reported, not judged.
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import pairwise

from hopcap import vrt
from hopcap.spool import Record

PS = 10**12
MAX_EXAMPLES = 5


def q32_for_rate(sample_rate_hz: int) -> int:
    """Picoseconds per sample in Q32.32, rounded as vrt49's overlay rounds it."""
    return (PS * 2**32 + sample_rate_hz // 2) // sample_rate_hz


def check(
    records: Iterable[Record],
    *,
    ps_per_sample_q32: int,
    expected_peer: tuple[str, int] | None = None,
) -> dict:
    """Judge every step between consecutive IF Data packets (pkt_count +1, no Context between)."""
    data = zero = checked = errors = non_increasing = crossings = 0
    examples: list[dict] = []
    residuals: list[int] = []
    prev: vrt.IfData | None = None
    for rec in records:
        if expected_peer and (rec.peer_ip, rec.peer_port) != expected_peer:
            continue
        try:
            pkt = vrt.parse(rec.data)
        except vrt.VrtError:
            continue
        if not isinstance(pkt, vrt.IfData):
            prev = None  # a Context starts a new capture
            continue
        data += 1
        if pkt.int_secs == 0 and pkt.frac_ps == 0:
            zero += 1
        if prev is not None and pkt.pkt_count == (prev.pkt_count + 1) & 0xF:
            checked += 1
            step = (pkt.int_secs - prev.int_secs) * PS + pkt.frac_ps - prev.frac_ps
            expected = prev.payload_words * ps_per_sample_q32 >> 32
            if step <= 0:
                non_increasing += 1
            if pkt.int_secs == prev.int_secs + 1:
                crossings += 1
                residuals.append(step - expected)
            elif step - expected not in (0, 1):
                errors += 1
                if len(examples) < MAX_EXAMPLES:
                    examples.append(
                        {
                            "pkt_count": pkt.pkt_count,
                            "int_secs": pkt.int_secs,
                            "frac_ps": pkt.frac_ps,
                            "step_ps": step,
                            "expected_ps": expected,
                        }
                    )
        prev = pkt
    return {
        "ps_per_sample_q32": ps_per_sample_q32,
        "data_packets": data,
        "zero_stamps": zero,
        "steps_checked": checked,
        "step_errors": errors,
        "non_increasing": non_increasing,
        "first_errors": examples,
        "second_crossings": crossings,
        "crossing_residual_ps_min": min(residuals) if residuals else None,
        "crossing_residual_ps_max": max(residuals) if residuals else None,
    }


def hop_spacing_residuals(hops, *, tick_hz: int) -> list[int]:
    """VRT first-sample spacing minus Tick executed-edge spacing (ps), consecutive hops that
    have both. Two independent clocks: the residual is the cross-check, report-only."""
    pts = [
        (h.first_sample_ps, h.tick_executed)
        for h in hops
        if h.first_sample_ps is not None and h.tick_executed is not None
    ]
    return [(b[0] - a[0]) - (b[1] - a[1]) * PS // tick_hz for a, b in pairwise(pts)]
