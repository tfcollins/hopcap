# SPDX-License-Identifier: MIT
"""Settle measurement from a fixed RF reference: synthetic tones with known analytic settle."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synth import stream_for_plan

from hopcap.associate import EdgeResult, associate
from hopcap.cli import load_plan
from hopcap.export import write_bundle, write_sums
from hopcap.plan import HopPlan, compile_plan
from hopcap.settle import analyse_run, instantaneous_frequency, settle_time

ROOT = Path(__file__).resolve().parents[2]
FS = 245_760_000
TICK_HZ = 100_000_000
MHZ = 1_000_000
REF_HZ = 2_452_500_000
TOL = 1000.0
PHASE_TOL_DEG = 5.0
AMP = 8000.0  # counts: well above int16 quantisation, well below full scale


def tone(
    n: int,
    offset_hz: float,
    *,
    df0_hz: float = 0.0,
    tau_s: float = 20e-6,
    snr_db: float = 50.0,
    phase_step_rad: float = 0.0,
    step_at_s: float | None = None,
    seed: int = 1,
) -> np.ndarray:
    """(N, 2) <i2 I/Q of a CW tone whose frequency is offset + df0 * exp(-t / tau), plus noise."""
    t = np.arange(n) / FS
    cycles = offset_hz * t + df0_hz * tau_s * (1 - np.exp(-t / tau_s))
    phase = 2 * np.pi * np.mod(cycles, 1.0)
    if step_at_s is not None:
        phase = phase + np.where(t >= step_at_s, phase_step_rad, 0.0)
    x = AMP * np.exp(1j * phase)
    if snr_db is not None:
        sigma = AMP / math.sqrt(2) / 10 ** (snr_db / 20)
        rng = np.random.default_rng(seed)
        x = x + sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return np.stack([np.round(x.real), np.round(x.imag)], axis=1).astype("<i2")


def noise_only(n: int, seed: int = 2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.round(rng.standard_normal((n, 2)) * 300).astype("<i2")


def analytic(df0, tau):
    """Frequency within tol: tau ln(df0 / tol).  Phase: the remaining integrated frequency error
    2 pi df0 tau exp(-t / tau) falls below the phase tolerance."""
    return tau * math.log(df0 / TOL), tau * math.log(
        2 * math.pi * df0 * tau / math.radians(PHASE_TOL_DEG)
    )


N = 491_520  # 2 ms at 245.76 MSPS, the example plan's capture


def test_instantaneous_frequency_of_a_clean_tone_is_its_offset():
    f = instantaneous_frequency(tone(65_536, 12.5e6, snr_db=None), FS, 256, 12.5e6)
    assert len(f) == 65_536
    assert np.median(np.abs(f[256:-256] - 12.5e6)) < 1.0


def test_exponential_settle_matches_analytic_value():
    df0, tau = 1e6, 20e-6
    r = settle_time(
        tone(N, 12.5e6, df0_hz=df0, tau_s=tau), FS, 12.5e6, TOL, phase_tol_deg=PHASE_TOL_DEG
    )
    assert r.ok, r.reason
    want_f, want_p = analytic(df0, tau)
    assert r.settle_s == pytest.approx(want_f, abs=2e-6)  # 138.2 us, within ~1.5 %
    assert r.phase_settle_s == pytest.approx(want_p, abs=2e-6)
    assert r.tone_snr_db == pytest.approx(50.0, abs=1.0)
    assert abs(r.offset_error_hz) < 10
    assert r.settled_at_first_sample is False


def test_negative_offset_and_downward_transient():
    df0, tau = -2e6, 10e-6
    r = settle_time(tone(N, -27.5e6, df0_hz=df0, tau_s=tau, seed=3), FS, -27.5e6, TOL)
    assert r.ok, r.reason
    assert r.settle_s == pytest.approx(analytic(abs(df0), tau)[0], abs=2e-6)


def test_noisier_capture_still_brackets_the_analytic_value():
    # At 40 dB the tail noise is ~90 Hz, and the deviation crosses tol at tol/tau = 50 Hz/us,
    # so a few-sigma excursion near the crossing moves the answer by a few us, never earlier.
    df0, tau = 1e6, 20e-6
    r = settle_time(tone(N, 12.5e6, df0_hz=df0, tau_s=tau, snr_db=40), FS, 12.5e6, TOL)
    assert r.ok, r.reason
    want = analytic(df0, tau)[0]
    assert want - 2e-6 <= r.settle_s <= want + 10e-6


def test_no_transient_settles_at_first_sample():
    r = settle_time(tone(N, 12.5e6), FS, 12.5e6, TOL)
    assert r.ok, r.reason
    assert r.settle_s <= r.resolution_s
    assert r.phase_settle_s <= r.resolution_s
    assert r.settled_at_first_sample is True


def test_no_tone_is_not_ok_with_reason():
    r = settle_time(noise_only(N), FS, 12.5e6, TOL)
    assert not r.ok
    assert r.reason.startswith("tone_not_detected")
    assert r.settle_s is None and r.phase_settle_s is None and r.final_offset_hz is None


def test_tone_outside_the_band_is_not_ok():
    r = settle_time(tone(N, 12.5e6), FS, 110e6, TOL)  # > 0.8 x fs/2 = 98.3 MHz
    assert not r.ok and r.reason.startswith("tone_out_of_band")
    assert r.settle_s is None


def test_tone_near_dc_is_not_ok():
    r = settle_time(tone(N, 0.0), FS, 0.0, TOL)
    assert not r.ok and r.reason.startswith("tone_near_dc")


def test_mirrored_tone_hints_at_iq_swap():
    r = settle_time(tone(N, -12.5e6), FS, 12.5e6, TOL)
    assert not r.ok and r.reason.startswith("tone_not_detected")
    assert "mirrored" in r.reason


def test_too_noisy_estimate_is_refused_not_reported():
    r = settle_time(tone(N, 12.5e6, df0_hz=1e6, snr_db=25), FS, 12.5e6, TOL)
    assert not r.ok and r.reason.startswith("estimate_too_noisy")
    assert r.settle_s is None


def test_transient_longer_than_the_capture_is_refused():
    r = settle_time(tone(N, 12.5e6, df0_hz=1e6, tau_s=1e-3), FS, 12.5e6, TOL)
    assert not r.ok
    assert r.reason.startswith(("tail_not_settled", "estimate_too_noisy"))


def test_phase_step_sets_phase_settle():
    t0 = 300e-6
    r = settle_time(
        tone(N, 12.5e6, phase_step_rad=1.0, step_at_s=t0), FS, 12.5e6, TOL, phase_tol_deg=5.0
    )
    assert r.ok, r.reason
    assert r.phase_settle_s == pytest.approx(t0, abs=2 * r.resolution_s)
    # The step is a ~1 rad / 1 us frequency spike, so frequency settles there too.
    assert r.settle_s == pytest.approx(t0, abs=3 * r.resolution_s)


# --------------------------------------------------------------------------------------------
# A real bundle written by export.write_bundle, with the hop npy files replaced by tones.

HOPS_MHZ = (2420, 2440, 2460, 2480)
CAPTURE_TICKS = 100_000  # 1 ms -> 245_760 samples per hop
TAUS = {0: 30e-6, 1: 10e-6, 2: 20e-6, 3: 40e-6, 4: 15e-6}  # by edge index; 0 is priming
DF0 = 1e6


def _bundle(tmp_path, *, drop_context_for=frozenset()):
    p = HopPlan(
        frequencies_hz=tuple(f * MHZ for f in HOPS_MHZ),
        period_ticks=2_000_000,
        settle_ticks=0,
        capture_ticks=CAPTURE_TICKS,
        pulse_ticks=1_000,
        lead_ticks=10_000,
        context_margin_ticks=1_000,
        name="settle-synth",
    )
    c = compile_plan(p, t0_ticks=0, tick_hz=TICK_HZ)
    recs = stream_for_plan(c, fs=FS, samples_per_packet=360, drop_context_for=drop_context_for)
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
    res = associate(recs, c, edges, sample_rate_hz=FS, samples_per_packet=360, epoch_base=0)
    out = write_bundle(
        tmp_path / "run",
        compiled=c,
        result=res,
        records=recs,
        sample_rate_hz=FS,
        provenance={"hopcap_commit": "test"},
        counters={},
    )
    manifest = json.loads((out / "manifest.json").read_text())
    for hop in manifest["hops"]:
        path = out / "hops" / f"hop_{hop['index']:04d}.npy"
        n = len(np.load(path))
        if n:
            offset = REF_HZ - hop["planned_hz"]
            np.save(path, tone(n, offset, df0_hz=DF0, tau_s=TAUS[hop["index"]], seed=hop["index"]))
    write_sums(out)
    return c, out


def test_analyse_run_on_an_exported_bundle(tmp_path):
    c, out = _bundle(tmp_path)
    run = analyse_run(out, REF_HZ, tol_hz=TOL)
    assert [h.index for h in run.hops] == [e.index for e in c.edges]
    for h in run.hops:
        assert h.result.ok, (h.index, h.result.reason)
        assert h.result.expected_offset_hz == REF_HZ - h.planned_hz
        assert h.result.settle_s == pytest.approx(analytic(DF0, TAUS[h.index])[0], abs=2e-6)
        assert h.open_after_edge_min_s == 0.0
    assert run.hops[0].priming and run.hops[0].planned_hz == 2_450 * MHZ
    assert run.hops[0].step_hz is None and run.hops[1].step_hz == -30 * MHZ
    s = run.summary
    assert s["non_priming_ok"] == 4
    want = sorted(analytic(DF0, TAUS[i])[0] for i in (1, 2, 3, 4))
    assert s["settle_s"]["max"] == pytest.approx(want[-1], abs=2e-6)
    assert s["settle_s"]["median"] == pytest.approx((want[1] + want[2]) / 2, abs=2e-6)
    assert len(s["priming"]) == 1 and s["priming"][0]["ok"]
    assert s["priming"][0]["settle_s"] == pytest.approx(analytic(DF0, TAUS[0])[0], abs=2e-6)
    # 2450 (priming) -> 2420 is a 30 MHz step, then 20 MHz steps up to 2480.
    assert s["by_step_hz"][str(30 * MHZ)]["settle_s"]["n"] == 1
    assert s["by_step_hz"][str(20 * MHZ)]["settle_s"]["n"] == 3
    json.dumps(run.as_dict(), allow_nan=False)


def test_analyse_run_carries_invalid_hops_as_not_ok(tmp_path):
    _c, out = _bundle(tmp_path, drop_context_for={2})
    run = analyse_run(out, REF_HZ, tol_hz=TOL)
    bad = run.hops[2]
    assert not bad.result.ok
    assert bad.disposition == "invalid" and "no_context" in bad.result.reason
    assert run.summary["non_priming_ok"] == 3
    json.dumps(run.as_dict(), allow_nan=False)


def test_analyse_run_with_a_2450_reference_refuses_the_priming_hop(tmp_path):
    _c, out = _bundle(tmp_path)
    run = analyse_run(out, 2_450_000_000, tol_hz=TOL)
    assert not run.hops[0].result.ok
    assert run.hops[0].result.reason.startswith("tone_near_dc")


def run_cli(*args):
    env = {"PATH": "", "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'third_party/tick/src'}"}
    return subprocess.run(
        [sys.executable, "-m", "hopcap.cli", *args], capture_output=True, text=True, env=env
    )


def test_cli_settle_table_and_json(tmp_path):
    _c, out = _bundle(tmp_path)
    r = run_cli("settle", str(out), "--ref-hz", str(REF_HZ))
    assert r.returncode == 0, r.stderr
    assert "settle  median" in r.stdout and "priming hop 0" in r.stdout
    r = run_cli("settle", str(out), "--ref-hz", str(REF_HZ), "--tol-hz", "1000", "--json")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["schema"] == "hopcap-settle/v1" and got["summary"]["non_priming_ok"] == 4


def test_cli_settle_fails_when_no_hop_is_measurable(tmp_path):
    _c, out = _bundle(tmp_path)
    r = run_cli("settle", str(out), "--ref-hz", str(3_000_000_000))  # out of band everywhere
    assert r.returncode == 1
    assert "tone_out_of_band" in r.stdout


def test_example_settle_plan_loads_and_compiles():
    plan, fs = load_plan(ROOT / "examples/plans/settle-measure.json")
    assert plan.settle_ticks == 0
    assert plan.capture_samples(sample_rate_hz=fs) == 491_520
    c = compile_plan(plan, t0_ticks=0, tick_hz=TICK_HZ)
    assert plan.period_ticks == 20_000_000  # the -slow plan's period
    # The recommended reference is in band and clear of DC for every LO, priming included.
    for e in c.edges:
        off = REF_HZ - e.commits_hz
        assert 1e6 <= abs(off) <= 0.8 * fs / 2
