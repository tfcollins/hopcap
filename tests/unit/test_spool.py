# SPDX-License-Identifier: MIT
from __future__ import annotations

import io

from hopcap.spool import HEADER_STRUCT, Record, iter_records, write_records


def test_record_round_trip_preserves_arrival_metadata_and_bytes():
    recs = [Record(mono_ns=1, real_ns=2, peer_ip="192.0.2.1", peer_port=4991, data=b"\x01\x02\x03"),
            Record(mono_ns=3, real_ns=4, peer_ip="10.0.0.7", peer_port=1, data=b"")]
    buf = io.BytesIO()
    write_records(buf, recs)
    assert buf.tell() == 2 * HEADER_STRUCT.size + 3
    buf.seek(0)
    assert list(iter_records(buf)) == recs


def test_truncated_record_is_reported_not_silently_dropped():
    buf = io.BytesIO()
    write_records(buf, [Record(mono_ns=1, real_ns=1, peer_ip="192.0.2.1", peer_port=1, data=b"abcd")])
    raw = buf.getvalue()[:-2]
    out = list(iter_records(io.BytesIO(raw), strict=False))
    assert out == []
    import pytest

    with pytest.raises(ValueError, match="truncated"):
        list(iter_records(io.BytesIO(raw)))
