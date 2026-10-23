# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import FakeSysfs

from hopcap.framer import COUNTERS, FramerConfig, configure, counters, delta


def test_configure_disables_then_programs_endpoint_then_enables_without_software_context():
    fs = FakeSysfs()
    cfg = FramerConfig(
        stream_id=0x01000000,
        mtu_bytes=1468,
        dest_ip="192.0.2.2",
        dest_port=4991,
        source_ip="192.0.2.1",
        source_port=4991,
        dest_mac="64:9d:99:6f:a9:6a",
        source_mac="02:00:00:00:00:01",
        ttl=64,
    )
    configure(fs, cfg)
    names = [w[0] for w in fs.writes]
    assert (
        names[:2] == ["v49_enable", "v49_context_enable"]
        and fs.writes[0][1] == "0"
        and fs.writes[1][1] == "0"
    )
    assert names.index("v49_udp_apply") > names.index("v49_udp_dest_mac")
    assert names[-1] == "v49_enable" and fs.writes[-1][1] == "1"
    assert fs.attrs["v49_context_enable"] == "0", (
        "Context is fabric-owned; software context stays off"
    )
    assert fs.attrs["v49_stream_id"] == "0x01000000" and fs.attrs["v49_packet_mtu"] == "1468"
    assert fs.attrs["v49_udp_dest_port"] == "4991"


def test_counters_and_delta():
    fs = FakeSysfs({"v49_tx_dropped_packets": 3, "v49_udp_packet_count": 100})
    before = counters(fs)
    assert set(before) == set(COUNTERS)
    fs.attrs["v49_udp_packet_count"] = "250"
    fs.attrs["v49_tx_dropped_packets"] = "3"
    d = delta(before, counters(fs))
    assert d["v49_udp_packet_count"] == 150 and d["v49_tx_dropped_packets"] == 0
