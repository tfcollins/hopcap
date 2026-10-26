# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from fakes import FakeSysfs, FakeTimeBase

from hopcap.framer import (
    COUNTERS,
    FramerConfig,
    TimeBaseError,
    configure,
    counters,
    delta,
    time_base_state,
)

NOW = 1_800_000_000.4  # host UTC when the run arms the time base; the arm is the next second
ARMED = 1_800_000_001


def _configure(fs, cfg=None):
    return configure(fs, cfg or FramerConfig(), now=lambda: NOW, sleep=lambda _s: None)


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
    _configure(fs, cfg)
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


def _index(fs, attr, value):
    return fs.writes.index((attr, value))


def test_configure_arms_the_time_base_while_the_framer_is_disabled():
    """The overlay boots with timestamps off (adi,timebase-start-disabled), so the framer stamps
    every packet with the zero legacy registers until software arms the time base. A previous
    run may have left it enabled, where the PPS source write is EBUSY."""
    fs = FakeSysfs(time_base=FakeTimeBase(mode=3, seconds=5))
    record = _configure(fs)
    assert (
        _index(fs, "v49_tb_mode", "0")
        < _index(fs, "v49_tb_pps_source", "corundum")
        < _index(fs, "v49_tb_seconds", str(ARMED))
        < _index(fs, "v49_tb_mode", "3")
        < len(fs.writes) - 1
    )
    assert fs.writes[-1] == ("v49_enable", "1")
    assert record["before"]["mode"] == 3 and record["before"]["seconds"] == 5
    assert record["armed_seconds"] == ARMED
    after = record["after_arm"]
    assert (
        after["mode"] == 3 and after["lock_state"] == "locked" and after["pps_source"] == "corundum"
    )
    assert after["seconds"] in (ARMED, ARMED + 1)


def test_arming_fails_closed_without_pps():
    fs = FakeSysfs(time_base=FakeTimeBase(pps=False, locked=False))
    with pytest.raises(TimeBaseError, match="no_pps"):
        _configure(fs)
    assert ("v49_enable", "1") not in fs.writes


def test_arming_fails_when_the_armed_seconds_never_load():
    fs = FakeSysfs(time_base=FakeTimeBase(loads_seconds=False, seconds=17))
    with pytest.raises(TimeBaseError, match=str(ARMED)):
        _configure(fs)
    assert ("v49_enable", "1") not in fs.writes


def test_arming_waits_for_lock_after_a_cold_start():
    fs = FakeSysfs(time_base=FakeTimeBase(locked=False))
    record = _configure(fs)
    assert record["before"]["lock_state"] == "no_pps"
    assert record["after_arm"]["lock_state"] == "locked"


def test_rearming_the_value_the_counter_already_holds_is_accepted():
    # The load at the next edge is invisible; the edge after it proves the counter runs on.
    fs = FakeSysfs(time_base=FakeTimeBase(seconds=ARMED))
    record = _configure(fs)
    assert record["after_arm"]["seconds"] == ARMED + 1


def test_non_strict_mode_and_time_base_off():
    fs = FakeSysfs()
    _configure(fs, FramerConfig(tb_strict=False))
    assert ("v49_tb_mode", "1") in fs.writes
    fs = FakeSysfs()
    assert _configure(fs, FramerConfig(time_base=False)) == {}
    assert not [w for w in fs.writes if w[0].startswith("v49_tb_")]


def test_time_base_state_decodes_the_status_word():
    tb = FakeTimeBase(seconds=42, mode=3)
    tb.missed, tb.sticky = 3, True
    st = time_base_state(FakeSysfs(time_base=tb))
    assert st["lock_state"] == "locked" and st["missed"] == 3 and st["resync_sticky"] is True
    assert st["mode"] == 3 and st["seconds"] == 42
    assert st["nominal_sps"] == 245_760_000 and st["ps_per_sample_q32"] == 17476266666667
