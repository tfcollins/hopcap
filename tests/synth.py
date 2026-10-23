# SPDX-License-Identifier: MIT
"""Synthetic hopcap streams: what the FPGA would emit for a compiled plan, as spool records."""

from __future__ import annotations

import numpy as np

from hopcap import vrt
from hopcap.plan import CompiledPlan, hz_to_q44_20
from hopcap.spool import Record

PS = 10**12
FPGA_IP, FPGA_PORT = "192.0.2.1", 4991


def _ts(sample_index: int, fs: int) -> tuple[int, int]:
    ps = sample_index * PS // fs
    return ps // PS, ps % PS


def stream_for_plan(
    compiled: CompiledPlan,
    *,
    fs: int = 245_760_000,
    samples_per_packet: int = 360,
    epoch_base: int = 0,
    drop_context_for: set[int] = frozenset(),
    drop_data_packets: dict[int, set[int]] | None = None,
    t0_mono_ns: int = 1_000_000_000,
) -> list[Record]:
    """One Context then ceil(capture_samples / spp) IF Data packets per edge.

    Sample timestamps are derived from the Tick schedule (edge rise + settle), so the
    generator is deterministic.  ``drop_context_for`` / ``drop_data_packets`` inject loss.
    """
    drop_data_packets = drop_data_packets or {}
    plan = compiled.plan
    tick_hz = compiled.tick_hz
    recs: list[Record] = []
    pkt_count = 0
    stream_id = 0x01000000
    for e in compiled.edges:
        epoch = epoch_base + e.index + 1
        open_tick = e.rise_ticks + plan.settle_ticks
        first_sample = open_tick * fs // tick_hz
        secs, frac = _ts(first_sample, fs)
        mono = t0_mono_ns + open_tick * (10**9 // tick_hz)
        if e.index not in drop_context_for:
            ctx = vrt.build_context(
                stream_id=stream_id,
                pkt_count=pkt_count & 0xF,
                int_secs=secs,
                frac_ps=frac,
                changed=True,
                rf_frequency_q44_20=hz_to_q44_20(e.commits_hz),
                hop_epoch=epoch,
            )
            recs.append(
                Record(mono_ns=mono, real_ns=mono, peer_ip=FPGA_IP, peer_port=FPGA_PORT, data=ctx)
            )
        pkt_count += 1
        n_samples = plan.capture_samples(sample_rate_hz=fs, tick_hz=tick_hz)
        n_pkts = -(-n_samples // samples_per_packet)
        for i in range(n_pkts):
            s0 = first_sample + i * samples_per_packet
            n = min(samples_per_packet, n_samples - i * samples_per_packet)
            iq = np.zeros((n, 2), dtype=np.int16)
            iq[:, 0] = (np.arange(n) + s0) & 0x7FFF
            iq[:, 1] = e.slot
            secs, frac = _ts(s0, fs)
            mono_i = mono + i * samples_per_packet * 10**9 // fs
            if i not in drop_data_packets.get(e.index, set()):
                pkt = vrt.build_if_data(
                    stream_id=stream_id,
                    pkt_count=pkt_count & 0xF,
                    int_secs=secs,
                    frac_ps=frac,
                    iq=iq,
                )
                recs.append(
                    Record(
                        mono_ns=mono_i,
                        real_ns=mono_i,
                        peer_ip=FPGA_IP,
                        peer_port=FPGA_PORT,
                        data=pkt,
                    )
                )
            pkt_count += 1
    return recs
