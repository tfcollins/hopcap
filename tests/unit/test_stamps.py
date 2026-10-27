# SPDX-License-Identifier: MIT
"""VRT timestamp checks over a spool: sample-exact steps inside a capture, PPS crossings, and the
all-zero stamps tron's boot-attempt3 emitted while the time base was never armed."""

from __future__ import annotations

import struct
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synth import FPGA_IP, FPGA_PORT, stream_for_plan
from test_associate import FS, SPP, TICK_HZ, compiled, edge_results

from hopcap import vrt
from hopcap.associate import associate
from hopcap.spool import Record
from hopcap.stamps import check, hop_spacing_residuals, q32_for_rate

Q32 = 17476266666667  # vrt49 ZU11EG overlay adi,ps-per-sample-q32 (245.76 MHz)


def _restamp(rec: Record, secs: int, frac: int) -> Record:
    data = bytearray(rec.data)
    struct.pack_into("!IQ", data, 16, secs, frac)
    return replace(rec, data=bytes(data))


def _is_data(rec: Record) -> bool:
    return isinstance(vrt.parse(rec.data), vrt.IfData)


def test_rate_scale_matches_the_overlay():
    assert q32_for_rate(FS) == Q32


def test_synthetic_stream_has_sample_exact_steps():
    c = compiled()
    recs = stream_for_plan(c, fs=FS, samples_per_packet=SPP)
    s = check(recs, ps_per_sample_q32=Q32)
    n_data = sum(_is_data(r) for r in recs)
    assert s["data_packets"] == n_data and s["zero_stamps"] == 0
    assert s["steps_checked"] == n_data - len(c.edges)  # the first packet after each Context
    assert s["step_errors"] == 0 and s["non_increasing"] == 0 and s["second_crossings"] == 0


def test_all_zero_stamps_fail_every_step():
    """What every hopcap run on boot-attempt3 recorded: TB_MODE off, legacy TS registers 0."""
    recs = [_restamp(r, 0, 0) if _is_data(r) else r for r in stream_for_plan(compiled(), fs=FS)]
    s = check(recs, ps_per_sample_q32=Q32)
    assert s["zero_stamps"] == s["data_packets"] > 0
    assert s["step_errors"] == s["steps_checked"] > 0
    assert s["non_increasing"] == s["steps_checked"]


def test_one_wrong_stamp_breaks_the_steps_into_and_out_of_it():
    recs = stream_for_plan(compiled(), fs=FS, samples_per_packet=SPP)
    i = [k for k, r in enumerate(recs) if _is_data(r)][10]
    p = vrt.parse(recs[i].data)
    recs[i] = _restamp(recs[i], p.int_secs, p.frac_ps + 1_000_000)
    s = check(recs, ps_per_sample_q32=Q32)
    assert s["step_errors"] == 2
    err = s["first_errors"][0]
    assert err["step_ps"] - err["expected_ps"] in (1_000_000, 1_000_001)


def test_a_lost_packet_is_not_a_step():
    c = compiled()
    full = check(stream_for_plan(c, fs=FS, samples_per_packet=SPP), ps_per_sample_q32=Q32)
    lossy = stream_for_plan(c, fs=FS, samples_per_packet=SPP, drop_data_packets={1: {5}})
    s = check(lossy, ps_per_sample_q32=Q32)
    assert s["step_errors"] == 0
    assert s["steps_checked"] == full["steps_checked"] - 2


def test_a_pps_edge_inside_a_capture_is_a_reported_crossing_not_an_error():
    """The time base re-anchors to every PPS edge, so a step that crosses a second carries the
    PPS period's offset from nominal (here 10 ns)."""
    iq = np.zeros((SPP, 2), dtype=np.int16)
    step = SPP * Q32 >> 32
    stamps = [(7, 10**12 - 2 * step), (7, 10**12 - step), (8, 10_000), (8, 10_000 + step)]
    recs = [
        Record(
            mono_ns=k,
            real_ns=k,
            peer_ip=FPGA_IP,
            peer_port=FPGA_PORT,
            data=vrt.build_if_data(
                stream_id=0x01000000, pkt_count=k, int_secs=sec, frac_ps=frac, iq=iq
            ),
        )
        for k, (sec, frac) in enumerate(stamps)
    ]
    s = check(recs, ps_per_sample_q32=Q32)
    assert s["steps_checked"] == 3 and s["step_errors"] == 0
    assert s["second_crossings"] == 1
    assert s["crossing_residual_ps_min"] == s["crossing_residual_ps_max"] == 10_000


def test_hop_spacing_tracks_the_tick_schedule():
    c = compiled()
    recs = stream_for_plan(c, fs=FS, samples_per_packet=SPP)
    res = associate(
        recs, c, edge_results(c), sample_rate_hz=FS, samples_per_packet=SPP, epoch_base=0
    )
    r = hop_spacing_residuals(res.hops, tick_hz=TICK_HZ)
    assert len(r) == len(c.edges) - 1
    assert all(abs(x) < 10**12 // FS + 1 for x in r), r  # within one sample
