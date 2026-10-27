# SPDX-License-Identifier: MIT
"""Receivers write the spool format; C and Python receivers must be byte-identical for the same input."""

from __future__ import annotations

import io
import shutil
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synth import stream_for_plan

from hopcap.plan import HopPlan, compile_plan
from hopcap.receiver import CReceiver, PyReceiver, ReceiverError, compile_udp_spool
from hopcap.spool import iter_records

MHZ = 1_000_000


def datagrams():
    p = HopPlan(
        frequencies_hz=(2_410 * MHZ, 2_420 * MHZ),
        period_ticks=200_000,
        settle_ticks=5_000,
        capture_ticks=4_000,
        pulse_ticks=1_000,
        lead_ticks=10_000,
        context_margin_ticks=1_000,
    )
    c = compile_plan(p, t0_ticks=0, tick_hz=100_000_000)
    return [r.data for r in stream_for_plan(c, samples_per_packet=360)]


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def send_all(port: int, payloads: list[bytes], *, pace_s: float = 0.0) -> tuple[str, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    for d in payloads:
        s.sendto(d, ("127.0.0.1", port))
        if pace_s:
            time.sleep(pace_s)
    src = s.getsockname()
    s.close()
    return src


@pytest.fixture(scope="module")
def spool_bin(tmp_path_factory):
    if shutil.which("cc") is None:
        pytest.skip("no C compiler")
    return compile_udp_spool(tmp_path_factory.mktemp("bin"))


def _check_spool(path: Path, sent: list[bytes], src: tuple[str, int]):
    recs = list(iter_records(io.BytesIO(path.read_bytes())))
    assert [r.data for r in recs] == sent
    assert all(r.peer_ip == src[0] and r.peer_port == src[1] for r in recs)
    monos = [r.mono_ns for r in recs]
    assert monos == sorted(monos) and monos[0] > 0
    assert all(r.real_ns > 1_600_000_000 * 10**9 for r in recs)


def test_python_receiver_spools_every_datagram_with_arrival_times(tmp_path):
    port = free_port()
    sent = datagrams()
    rx = PyReceiver(bind=("127.0.0.1", port), spool_path=tmp_path / "py.spool")
    rx.start()
    src = send_all(port, sent)
    time.sleep(0.2)
    summary = rx.stop()
    _check_spool(tmp_path / "py.spool", sent, src)
    assert summary["spool_records"] == len(sent) and summary["spool_bytes"] == sum(
        len(d) for d in sent
    )
    assert summary["context_packets"] == 3 and summary["data_packets"] == len(sent) - 3
    assert summary["malformed"] == 0


def test_c_receiver_spools_every_datagram_and_reports_summary(tmp_path, spool_bin):
    port = free_port()
    sent = datagrams()
    rx = CReceiver(bind=("127.0.0.1", port), spool_path=tmp_path / "c.spool", binary=spool_bin)
    rx.start()  # returns once the helper printed `ready`
    src = send_all(port, sent)
    time.sleep(0.2)
    summary = rx.stop()  # SIGTERM -> drain -> JSON summary
    _check_spool(tmp_path / "c.spool", sent, src)
    assert summary["spool_records"] == len(sent)
    assert summary["spool_bytes"] == sum(len(d) for d in sent)
    assert summary["context_packets"] == 3 and summary["data_packets"] == len(sent) - 3
    assert summary["malformed"] == 0 and summary["rcvbuf"] > 0


def test_c_receiver_reset_clears_counters(tmp_path, spool_bin):
    port = free_port()
    sent = datagrams()
    rx = CReceiver(bind=("127.0.0.1", port), spool_path=tmp_path / "c.spool", binary=spool_bin)
    rx.start()
    send_all(port, sent[:4])
    time.sleep(0.1)
    rx.reset()  # SIGUSR1: counters restart, spool keeps growing
    send_all(port, sent[4:])
    time.sleep(0.1)
    summary = rx.stop()
    assert summary["spool_records"] == len(sent) - 4
    recs = list(iter_records(io.BytesIO((tmp_path / "c.spool").read_bytes())))
    assert len(recs) == len(sent)


def test_c_receiver_max_bytes_fails_closed(tmp_path, spool_bin):
    port = free_port()
    sent = datagrams()
    rx = CReceiver(
        bind=("127.0.0.1", port), spool_path=tmp_path / "c.spool", binary=spool_bin, max_bytes=2000
    )
    rx.start()
    send_all(port, sent)
    time.sleep(0.2)
    with pytest.raises(ReceiverError, match="max-bytes"):
        rx.stop()


def test_bind_failure_is_reported_at_start(tmp_path, spool_bin):
    port = free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", port))
    try:
        rx = CReceiver(bind=("127.0.0.1", port), spool_path=tmp_path / "c.spool", binary=spool_bin)
        with pytest.raises(ReceiverError, match="bind"):
            rx.start()
    finally:
        blocker.close()


def test_python_and_c_spools_are_byte_identical_modulo_timestamps(tmp_path, spool_bin):
    sent = datagrams()
    port_py, port_c = free_port(), free_port()
    py = PyReceiver(bind=("127.0.0.1", port_py), spool_path=tmp_path / "py.spool")
    c = CReceiver(bind=("127.0.0.1", port_c), spool_path=tmp_path / "c.spool", binary=spool_bin)
    py.start()
    c.start()
    send_all(port_py, sent)
    send_all(port_c, sent)
    time.sleep(0.2)
    py.stop()
    c.stop()
    a = [
        (r.data, r.peer_ip) for r in iter_records(io.BytesIO((tmp_path / "py.spool").read_bytes()))
    ]
    b = [(r.data, r.peer_ip) for r in iter_records(io.BytesIO((tmp_path / "c.spool").read_bytes()))]
    assert a == b
