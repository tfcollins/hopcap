# SPDX-License-Identifier: MIT
"""Pure-Python VITA 49.2 parser/builder, cross-checked against libvrt49 when it is built."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

from hopcap import vrt

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "build/vrt49/src/libvrt49/libvrt49.so"
STREAM = 0x01000000
F = 2_430_000_000 << 20


def golden():
    if not LIB.exists():
        pytest.skip("libvrt49.so not built (cmake -DBUILD_SHARED_LIBS=ON ...)")
    os.environ.setdefault("VRT49_LIB", str(LIB))
    sys.path.insert(0, str(ROOT / "third_party/vrt49/hdl/sim/cocotb"))
    import vrt49_codec  # noqa: PLC0415

    return vrt49_codec


def test_if_data_round_trip_and_ci16_decode():
    iq = np.array([[1, -2], [300, -400], [32767, -32768]], dtype=np.int16)
    pkt = vrt.build_if_data(stream_id=STREAM, pkt_count=5, int_secs=12, frac_ps=34, iq=iq)
    p = vrt.parse(pkt)
    assert isinstance(p, vrt.IfData)
    assert p.stream_id == STREAM and p.pkt_count == 5
    assert p.int_secs == 12 and p.frac_ps == 34
    assert p.payload_words == 3
    np.testing.assert_array_equal(p.iq(), iq)


def test_if_data_matches_libvrt49_prologue():
    c = golden()
    spec = c.FrameSpec(stream_id=STREAM, pkt_count=9, int_secs=0x10203040, frac_ps=0x50607080, payload_words=16)
    payload = c.make_ramp_payload_be(16, start=0x1000)
    pkt = c.build_packet(spec, payload)
    p = vrt.parse(pkt)
    assert isinstance(p, vrt.IfData)
    assert (p.stream_id, p.pkt_count, p.int_secs, p.frac_ps, p.payload_words) == (STREAM, 9, 0x10203040, 0x50607080, 16)
    assert p.class_oui == spec.class_oui and p.packet_class_code == spec.packet_class_code
    assert p.payload_bytes == payload
    # our builder reproduces the same bytes given the same words
    ours = vrt.build_if_data_words(stream_id=STREAM, pkt_count=9, int_secs=0x10203040, frac_ps=0x50607080,
                                   payload_be=payload, class_oui=spec.class_oui,
                                   info_class_code=spec.info_class_code,
                                   packet_class_code=spec.packet_class_code)
    assert ours == pkt


def test_context_matches_libvrt49_and_walks_cif_fields():
    c = golden()
    spec = c.FrameSpec(stream_id=STREAM, pkt_count=3, int_secs=7, frac_ps=8)
    pkt = c.build_context_packet(spec, changed=True, bandwidth=1 << 20, rf_frequency=F, overrange_count=2,
                                 sample_rate=245_760_000 << 20, state_event=0x9, signal_format=0xA0B,
                                 discrete_io_32=0xDEAD, info_source_id=42)
    p = vrt.parse(pkt)
    assert isinstance(p, vrt.Context)
    assert p.changed is True and p.stream_id == STREAM and p.pkt_count == 3
    assert p.fields["rf_reference_frequency"] == F
    assert p.rf_frequency_hz == 2_430_000_000
    assert p.fields["bandwidth"] == 1 << 20
    assert p.fields["sample_rate"] == 245_760_000 << 20
    assert p.fields["over_range_count"] == 2
    assert p.fields["state_event"] == 0x9
    assert p.fields["signal_data_format"] == 0xA0B
    assert p.fields["discrete_io_32"] == 0xDEAD
    assert p.fields["info_source_id"] == 42
    assert p.hop_epoch == 42
    ours = vrt.build_context(stream_id=STREAM, pkt_count=3, int_secs=7, frac_ps=8, changed=True,
                             rf_frequency_q44_20=F, hop_epoch=42, bandwidth_q44_20=1 << 20,
                             sample_rate_q44_20=245_760_000 << 20, over_range_count=2, state_event=0x9,
                             signal_data_format=0xA0B, discrete_io_32=0xDEAD)
    assert ours == pkt


def test_context_refresh_flag_and_non_integer_frequency():
    pkt = vrt.build_context(stream_id=STREAM, pkt_count=0, int_secs=0, frac_ps=0, changed=False,
                            rf_frequency_q44_20=(2_430_000_000 << 20) | 5, hop_epoch=1)
    p = vrt.parse(pkt)
    assert p.changed is False
    with pytest.raises(vrt.VrtError, match="fractional"):
        _ = p.rf_frequency_hz
    assert p.rf_frequency_hz_float == pytest.approx(2_430_000_000 + 5 / 2**20)


def test_parse_rejects_truncated_and_unknown_packets():
    with pytest.raises(vrt.VrtError, match="short"):
        vrt.parse(b"\x00" * 10)
    bad = bytearray(vrt.build_context(stream_id=STREAM, pkt_count=0, int_secs=0, frac_ps=0, changed=True,
                                      rf_frequency_q44_20=F, hop_epoch=1))
    bad[0] = (0x3 << 4) | (bad[0] & 0x0F)  # packet type 3 = IF data without SID... not emitted here
    with pytest.raises(vrt.VrtError, match="type"):
        vrt.parse(bytes(bad))


def test_cif_walker_sizes_cover_the_standard_table():
    # Change indicator consumes no words; GPS/INS/ephemeris are fixed-size blocks.
    assert vrt.cif0_field_words(31) == 0
    assert vrt.cif0_field_words(27) == 2
    assert vrt.cif0_field_words(22) == 1
    assert vrt.cif0_field_words(14) == 11
    assert vrt.cif0_field_words(12) == 13
    assert vrt.cif0_field_words(1) == 0 and vrt.cif0_field_words(2) == 0
