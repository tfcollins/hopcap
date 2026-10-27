# SPDX-License-Identifier: MIT
"""End-to-end orchestration with fakes: Tick model, hop-controller fake, framer fake, stager
fake, and a receiver fake that produces the stream the FPGA would have emitted."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import FakeMmio, FakeStager, FakeSysfs, FakeTickDevice
from synth import stream_for_plan

from hopcap.hopctrl import HopCtrlRegs
from hopcap.plan import FhmPipelineModel, HopPlan
from hopcap.run import Components, RunConfig, run_live
from hopcap.spool import write_records

MHZ = 1_000_000


class FakeReceiver:
    """Writes, at stop(), the spool a perfect FPGA would have produced for the compiled plan."""

    def __init__(self, spool_path: Path, fs: int):
        self.spool_path, self.fs = spool_path, fs
        self.compiled = None
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        recs = stream_for_plan(self.compiled, fs=self.fs, samples_per_packet=360, epoch_base=0)
        with self.spool_path.open("wb") as fp:
            write_records(fp, recs)
        return {
            "spool_records": len(recs),
            "data_packets": len(recs) - len(self.compiled.edges),
            "context_packets": len(self.compiled.edges),
            "malformed": 0,
            "rcvbuf": 1,
            "peers": [],
        }


def test_run_live_produces_a_verified_bundle_with_every_hop_ok(tmp_path):
    regs = HopCtrlRegs.from_json()
    mmio = FakeMmio(regs)
    tick = FakeTickDevice()
    framer = FakeSysfs()
    plan = HopPlan(
        frequencies_hz=(2_410 * MHZ, 2_430 * MHZ, 2_470 * MHZ),
        period_ticks=2_000_000,
        settle_ticks=20_000,
        capture_ticks=100_000,
        pulse_ticks=100_000,
        lead_ticks=500_000,
    )
    pipeline = FhmPipelineModel(init_hz=plan.init_frequency_hz)
    stager = FakeStager(pipeline)
    rx = FakeReceiver(tmp_path / "out" / "datagrams.spool", fs=245_760_000)

    def on_compiled(c):
        rx.compiled = c
        mmio.mem[regs.EPOCH] = len(c.edges)  # the fake controller "ran" every event

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
        host_counters=lambda: {"rx_missed_errors": 0},
        sleep=lambda s: None,
    )
    cfg = RunConfig(
        plan=plan,
        out_dir=tmp_path / "out",
        sample_rate_hz=245_760_000,
        provenance={"hopcap_commit": "test"},
        expected_peer=("192.0.2.1", 4991),
    )
    report = run_live(cfg, comps)

    assert rx.started and rx.stopped
    assert report.disposition == "ok"
    assert [h["disposition"] for h in report.manifest["hops"]] == ["ok"] * 4
    assert report.manifest["counters"]["framer"]["v49_tx_dropped_packets"] == 0
    assert report.manifest["counters"]["host_before"]["rx_missed_errors"] == 0
    assert report.manifest["tick"]["late_count_delta"] == 0
    assert report.manifest["hopctrl"]["after"]["epoch"] == 4
    assert mmio.enabled is False, "controller left disabled (gate open) after the run"
    assert stager.staged == list(plan.frequencies_hz)
    assert (tmp_path / "out" / "run.sigmf-meta").exists()
    assert json.loads((tmp_path / "out" / "manifest.json").read_text())["disposition"] == "ok"
    # the bundle verifies after run_live's manifest amendments, and ignores operator files
    import subprocess
    import sys as _sys

    (tmp_path / "out" / "launcher.log").write_text("operator log, keeps growing\n")
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        [_sys.executable, str(root / "scripts/verify_evidence.py"), "run", str(tmp_path / "out")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    # framer counters are taken before and after, and the framer was enabled for the run
    assert ("v49_enable", "1") in framer.writes
    # the time base was armed before the stream started, and the run's stamps were judged
    tb = report.manifest["time_base"]
    assert tb["armed"]["after_arm"]["lock_state"] == "locked"
    assert framer.writes.index(("v49_tb_mode", "3")) < framer.writes.index(("v49_enable", "1"))
    assert tb["after"]["lock_state"] == "locked" and tb["after"]["missed"] == 0
    assert tb["stamps"]["zero_stamps"] == 0 and tb["stamps"]["step_errors"] == 0
    assert tb["stamps"]["steps_checked"] > 0
    assert len(tb["hop_spacing_residual_ps"]) == 3


def test_run_live_fails_closed_when_hop_controller_id_is_wrong(tmp_path):
    import pytest

    from hopcap.hopctrl import HopCtrlError

    regs = HopCtrlRegs.from_json()
    mmio = FakeMmio(regs)
    mmio.mem[regs.ID] = 0x484F5031
    tick = FakeTickDevice()
    plan = HopPlan(
        frequencies_hz=(2_410 * MHZ,), period_ticks=2_000_000, settle_ticks=1, capture_ticks=1
    )
    pipeline = FhmPipelineModel(init_hz=plan.init_frequency_hz)
    comps = Components(
        tick_read=tick.read_attr,
        tick_write=tick.write_attr,
        mmio=mmio,
        framer=FakeSysfs(),
        stager=FakeStager(pipeline),
        receiver=FakeReceiver(tmp_path / "x.spool", 1),
        pipeline=pipeline,
        sleep=lambda s: None,
    )
    with pytest.raises(HopCtrlError, match="HOP1"):
        run_live(RunConfig(plan=plan, out_dir=tmp_path / "out", sample_rate_hz=245_760_000), comps)
