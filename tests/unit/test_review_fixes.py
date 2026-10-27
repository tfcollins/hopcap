# SPDX-License-Identifier: MIT
"""Regression tests for the whole-branch review findings (2026-10-23)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import FakeMmio, FakeStager, FakeSysfs, FakeTickDevice
from synth import stream_for_plan

from hopcap.hopctrl import HopCtrlRegs
from hopcap.plan import FhmPipelineModel, HopPlan, PlanError, compile_plan
from hopcap.run import Components, RunConfig, run_live
from hopcap.scheduler import TickSession
from hopcap.spool import Record, write_records

MHZ = 1_000_000
TICK_HZ = 100_000_000
ROOT = Path(__file__).resolve().parents[2]


def _plan(n=3):
    return HopPlan(
        frequencies_hz=tuple(2_410 * MHZ + i * 10 * MHZ for i in range(n)),
        period_ticks=2_000_000,
        settle_ticks=20_000,
        capture_ticks=100_000,
        pulse_ticks=100_000,
        lead_ticks=500_000,
    )


# ---- #1 pipeline advanced by the session, not by a helpful fake ---------------------------
def test_session_drives_pipeline_with_a_dumb_stager():
    dev = FakeTickDevice()
    p = _plan()
    pipeline = FhmPipelineModel(init_hz=p.init_frequency_hz)
    stager = FakeStager(pipeline)
    s = TickSession(dev.read_attr, dev.write_attr, lookahead_hops=2, sleep=lambda _s: None)
    c = compile_plan(p, t0_ticks=dev.now, tick_hz=TICK_HZ)
    results = s.run(c, stager, pipeline, after_edge=lambda ex, hz: setattr(stager, "pll", hz))
    assert [r.commits_hz for r in results] == [e.commits_hz for e in c.edges]
    assert pipeline.expected_hz == c.edges[-1].commits_hz


# ---- #2 teardown order: framer off before the receiver is stopped; cleanup on exceptions ---
class OrderReceiver:
    def __init__(self, framer: FakeSysfs, spool: Path, compiled_box: dict):
        self.framer, self.spool, self.box = framer, spool, compiled_box
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        assert self.framer.attrs["v49_enable"] == "0", (
            "receiver stopped while the framer still streams"
        )
        self.stopped = True
        recs = (
            stream_for_plan(self.box["c"], fs=245_760_000, samples_per_packet=360)
            if "c" in self.box
            else []
        )
        with self.spool.open("wb") as fp:
            write_records(fp, recs)
        return {
            "spool_records": len(recs),
            "data_packets": 0,
            "context_packets": 0,
            "malformed": 0,
            "rcvbuf": 1,
            "peers": [],
        }


def _components(tmp_path, *, tick=None, mmio=None, framer=None, rx=None, box=None):
    regs = HopCtrlRegs.from_json()
    mmio = mmio or FakeMmio(regs)
    tick = tick or FakeTickDevice()
    framer = framer or FakeSysfs()
    box = box if box is not None else {}
    p = _plan()
    pipeline = FhmPipelineModel(init_hz=p.init_frequency_hz)
    stager = FakeStager(pipeline)
    rx = rx or OrderReceiver(framer, tmp_path / "out" / "datagrams.spool", box)

    def on_compiled(c):
        box["c"] = c
        mmio.mem[regs.EPOCH] = len(c.edges)

    comps = Components(
        tick_read=tick.read_attr,
        tick_write=tick.write_attr,
        mmio=mmio,
        framer=framer,
        stager=stager,
        receiver=rx,
        pipeline=pipeline,
        on_compiled=on_compiled,
        after_edge=lambda ex, hz: setattr(stager, "pll", hz),
        sleep=lambda s: None,
    )
    return p, comps, mmio, framer, stager, rx


def test_framer_is_disabled_before_receiver_stop_and_hop_controller_disabled_last(tmp_path):
    p, comps, mmio, framer, _stager, rx = _components(tmp_path)
    report = run_live(
        RunConfig(plan=p, out_dir=tmp_path / "out", sample_rate_hz=245_760_000), comps
    )
    assert rx.stopped and report.disposition == "ok"
    names = [w[0] for w in framer.writes]
    assert names[-1] == "v49_enable" and framer.writes[-1][1] == "0"
    assert mmio.enabled is False


def test_exception_during_hop_still_cleans_up(tmp_path):
    p, comps, mmio, framer, stager, rx = _components(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("tick died")

    comps.tick_read = boom
    with pytest.raises(RuntimeError, match="tick died"):
        run_live(RunConfig(plan=p, out_dir=tmp_path / "out", sample_rate_hz=245_760_000), comps)
    assert framer.attrs["v49_enable"] == "0"
    assert mmio.enabled is False
    assert rx.stopped is True or rx.started is False
    assert stager.enabled is False


# ---- #8 no gapless flood while armed: program() leaves idle_open = 0 ----------------------
def test_program_closes_gate_while_idle_and_enabled():
    from hopcap.hopctrl import HopController
    from hopcap.plan import SlotTable

    regs = HopCtrlRegs.from_json()
    m = FakeMmio(regs)
    p = _plan()
    HopController(m, regs).program(p, SlotTable.from_plan(p))
    assert m.enabled and m.idle_open is False
    assert HopController(m, regs).status().gate_open is False


# ---- #5 upload over a serial console: chunked, staged, hash-verified ------------------------
def test_upload_file_is_chunked_and_verified():
    from hopcap import transport

    class Sh:
        def __init__(self):
            self.cmds = []

        def run(self, cmd, *, timeout_s=15.0):
            self.cmds.append(cmd)
            return ""

    sh = Sh()
    transport.upload_file(sh, transport.HELPER_SRC, "/tmp/hopcap_target.py")
    assert all(len(c) <= 400 for c in sh.cmds), max(len(c) for c in sh.cmds)
    assert any("sha256sum -c" in c for c in sh.cmds)
    assert any("base64 -d" in c for c in sh.cmds)
    assert len(sh.cmds) > 10


# ---- #6 staging floor and adaptive lead -------------------------------------------------------
def test_compile_plan_rejects_period_below_host_staging_floor():
    p = _plan()
    with pytest.raises(PlanError, match="staging"):
        compile_plan(p, t0_ticks=0, tick_hz=TICK_HZ, min_period_ticks=3_000_000)
    compile_plan(p, t0_ticks=0, tick_hz=TICK_HZ, min_period_ticks=1_000_000)


def test_run_live_extends_lead_to_cover_measured_transport_latency(tmp_path):
    """A slowish transport (2 ms per attribute access) must not make edge 0 late: the lead is
    stretched to cover setup, and the plan still fits its 20 ms period."""
    tick = FakeTickDevice(step=200_000)
    p, comps, _mmio, _framer, _stager, _rx = _components(tmp_path, tick=tick)
    report = run_live(
        RunConfig(plan=p, out_dir=tmp_path / "out", sample_rate_hz=245_760_000), comps
    )
    assert report.compiled.plan.lead_ticks > p.lead_ticks
    assert report.manifest["tick"]["transport_rtt_ticks"] >= 200_000
    assert all(h["tick_status"] == "EXECUTED" for h in report.manifest["hops"])


def test_run_live_fails_closed_when_transport_cannot_stage_within_the_period(tmp_path):
    """20 ms per access against a 20 ms period: the host could never stage in time."""
    tick = FakeTickDevice(step=2_000_000)
    p, comps, mmio, framer, _stager, _rx = _components(tmp_path, tick=tick)
    with pytest.raises(PlanError, match="staging floor"):
        run_live(RunConfig(plan=p, out_dir=tmp_path / "out", sample_rate_hz=245_760_000), comps)
    assert framer.attrs["v49_enable"] == "0" and mmio.enabled is False, (
        "fail-closed path still cleans up"
    )


# ---- #7 export slices each hop by its window, not until the next Context -----------------------
def test_export_does_not_absorb_trailing_flood_into_last_hop(tmp_path):
    import numpy as np

    from hopcap import vrt
    from hopcap.associate import EdgeResult, associate
    from hopcap.export import write_bundle

    p = _plan(2)
    c = compile_plan(p, t0_ticks=0, tick_hz=TICK_HZ)
    recs = stream_for_plan(c, fs=245_760_000, samples_per_packet=360)
    last = recs[-1]
    flood = [
        Record(
            mono_ns=last.mono_ns + 10**9 + i * 1_000,
            real_ns=0,
            peer_ip="192.0.2.1",
            peer_port=4991,
            data=vrt.build_if_data(
                stream_id=0x01000000,
                pkt_count=i & 0xF,
                int_secs=999,
                frac_ps=i * 10**9,
                iq=np.zeros((360, 2), dtype=np.int16),
            ),
        )
        for i in range(50)
    ]
    edges = [
        EdgeResult(
            index=e.index,
            slot=e.slot,
            commits_hz=e.commits_hz,
            requested_ticks=e.rise_ticks,
            executed_ticks=e.rise_ticks,
            status="EXECUTED",
            readback_hz=e.commits_hz,
            stage_late=False,
        )
        for e in c.edges
    ]
    res = associate(
        recs + flood, c, edges, sample_rate_hz=245_760_000, samples_per_packet=360, epoch_base=0
    )
    out = write_bundle(
        tmp_path / "out",
        compiled=c,
        result=res,
        records=recs + flood,
        sample_rate_hz=245_760_000,
        provenance={},
        counters={},
    )
    last_hop = res.hops[-1]
    arr = np.load(out / "hops" / f"hop_{last_hop.index:04d}.npy")
    assert len(arr) == last_hop.samples


# ---- #9 orphan window must sit between its neighbours, never the pre-run idle stream -----------
def test_lost_context_does_not_claim_the_pre_run_stream():
    import numpy as np

    from hopcap import vrt
    from hopcap.associate import EdgeResult, associate

    p = _plan(3)
    c = compile_plan(p, t0_ticks=0, tick_hz=TICK_HZ)
    pre = [
        Record(
            mono_ns=i * 1_000,
            real_ns=0,
            peer_ip="192.0.2.1",
            peer_port=4991,
            data=vrt.build_if_data(
                stream_id=0x01000000,
                pkt_count=i & 0xF,
                int_secs=0,
                frac_ps=i * 10**6,
                iq=np.zeros((360, 2), dtype=np.int16),
            ),
        )
        for i in range(500)
    ]
    recs = stream_for_plan(c, fs=245_760_000, samples_per_packet=360, drop_context_for={2})
    edges = [
        EdgeResult(
            index=e.index,
            slot=e.slot,
            commits_hz=e.commits_hz,
            requested_ticks=e.rise_ticks,
            executed_ticks=e.rise_ticks,
            status="EXECUTED",
            readback_hz=e.commits_hz,
            stage_late=False,
        )
        for e in c.edges
    ]
    res = associate(
        pre + recs, c, edges, sample_rate_hz=245_760_000, samples_per_packet=360, epoch_base=0
    )
    bad = res.hops[2]
    assert bad.disposition == "invalid" and "no_context" in bad.reasons
    expected = p.capture_samples(sample_rate_hz=245_760_000, tick_hz=TICK_HZ)
    assert bad.samples == expected, (
        "orphan window must be the hop's own data, not the 500-packet pre-run stream"
    )


# ---- #4 BD fragment must not delete nets shared with other framer pins -------------------------
def test_bd_fragment_disconnects_pins_instead_of_deleting_shared_nets():
    tcl = (ROOT / "hdl/system_bd_fabric_hop.tcl").read_text()
    assert "disconnect_bd_net" in tcl
    assert "delete_bd_objs $net" not in tcl
