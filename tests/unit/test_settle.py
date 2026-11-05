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
from hopcap.settle import _settle_index, analyse_run, instantaneous_frequency, settle_time

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
    dev_hz: np.ndarray | None = None,
    seed: int = 1,
    conj: bool = False,
) -> np.ndarray:
    """(N, 2) <i2 I/Q of a CW tone whose frequency is offset + df0 * exp(-t / tau) (+ an
    arbitrary per-sample deviation ``dev_hz``), plus noise.  ``conj`` swaps the spectrum."""
    t = np.arange(n) / FS
    cycles = offset_hz * t + df0_hz * tau_s * (1 - np.exp(-t / tau_s))
    if dev_hz is not None:
        cycles = cycles + np.cumsum(dev_hz) / FS
    phase = 2 * np.pi * np.mod(cycles, 1.0)
    if step_at_s is not None:
        phase = phase + np.where(t >= step_at_s, phase_step_rad, 0.0)
    x = AMP * np.exp(1j * phase)
    if conj:
        x = np.conj(x)
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
    # Nothing ever leaves the tolerance, edges included: exactly sample 0, flagged censored.
    assert r.settle_s == 0.0 and r.phase_settle_s == 0.0
    assert r.settled_at_first_sample is True
    assert r.resolution_s == pytest.approx(2 * 256 / FS)  # two cascaded 256-sample boxcars


def test_settled_at_first_sample_threshold_is_two_windows():
    fast = settle_time(tone(N, 12.5e6, df0_hz=1e6, tau_s=0.1e-6), FS, 12.5e6, TOL)
    assert fast.ok and fast.settled_at_first_sample is True  # 0.7 us transient, inside the blur
    df0, tau = 1e6, 1e-6  # analytic 6.9 us, well beyond 2 x window = 2.08 us
    slow = settle_time(tone(N, 12.5e6, df0_hz=df0, tau_s=tau), FS, 12.5e6, TOL)
    assert slow.ok and slow.settled_at_first_sample is False
    assert slow.settle_s == pytest.approx(analytic(df0, tau)[0], abs=slow.resolution_s)


def test_settle_index_is_after_the_last_excursion():
    assert _settle_index(np.array([5.0, 0.0, 5.0, 0.0, 0.0]), 1.0) == 3
    assert _settle_index(np.array([0.0, 0.5, -0.9]), 1.0) == 0
    assert _settle_index(np.array([0.0, 0.0, 2.0]), 1.0) == 3  # out at the end: len(dev)
    assert _settle_index(np.array([-2.0, 1.0, -1.0]), 1.0) == 1  # |dev| == tol is within


def test_ringing_transient_settles_after_its_last_excursion():
    # Damped ringing: 1 MHz x exp(-t / 20 us) x cos(2 pi 50 kHz t) re-crosses the band every
    # 10 us.  Settle is the last exit from +/-tol, computed on the exact model, not the first
    # entry into the band.
    t = np.arange(N) / FS
    dev = 1e6 * np.exp(-t / 20e-6) * np.cos(2 * np.pi * 50e3 * t)
    want = _settle_index(dev, TOL) / FS
    first_entry = np.flatnonzero(np.abs(dev) <= TOL)[0] / FS
    assert want - first_entry > 100e-6  # the rule matters for this signal
    r = settle_time(tone(N, 12.5e6, dev_hz=dev), FS, 12.5e6, TOL)
    assert r.ok, r.reason
    assert r.settle_s == pytest.approx(want, abs=2 * r.resolution_s)


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
    assert not r.ok and r.settle_s is None


def _tail_ramp(hz_across_tail: float) -> np.ndarray:
    """Frequency ramp whose change across the 0.25 tail (N/4 samples) is ``hz_across_tail``."""
    return hz_across_tail / (N // 4) * np.arange(N)


def test_slow_drift_in_the_tail_is_tail_not_settled():
    # 800 Hz across the tail: halves differ by ~400 Hz > tol/4, yet every tail sample is within
    # +/-400 Hz of the median and the noise is tiny, so only the drift test can refuse it.
    r = settle_time(tone(N, 12.5e6, dev_hz=_tail_ramp(800.0)), FS, 12.5e6, TOL)
    assert not r.ok and r.reason.startswith("tail_not_settled"), r.reason
    assert r.settle_s is None


def test_late_excursion_in_the_tail_is_refused_not_reported():
    # Review I1: one spike late in the tail used to give ok with settle_s ~ 1.8 ms.
    r = settle_time(tone(N, 12.5e6, phase_step_rad=1.0, step_at_s=1.8e-3), FS, 12.5e6, TOL)
    assert not r.ok and r.reason.startswith("unsettled_in_tail"), r.reason
    assert r.settle_s is None


def test_phase_unsettled_in_the_tail_is_refused():
    # 400 Hz across the tail: frequency within tol and drift below tol/4, but the quadratic
    # phase leaves ~6 deg of residual against the tail's line fit.
    r = settle_time(tone(N, 12.5e6, dev_hz=_tail_ramp(400.0)), FS, 12.5e6, TOL, phase_tol_deg=2.0)
    assert not r.ok and r.reason.startswith("phase_unsettled_in_tail"), r.reason
    assert r.settle_s is None and r.phase_settle_s is None


@pytest.mark.parametrize("window", [0, -4, 2.5])
def test_window_is_validated_up_front(window):
    with pytest.raises(ValueError, match="window"):
        settle_time(tone(1024, 12.5e6), FS, 12.5e6, TOL, window=window)


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


def _bundle(tmp_path, *, drop_context_for=frozenset(), taus=None, conj=False):
    taus = taus or TAUS
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
            np.save(
                path,
                tone(n, offset, df0_hz=DF0, tau_s=taus[hop["index"]], seed=hop["index"], conj=conj),
            )
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
    assert s["non_priming_ok"] == 4 and s["n_measured"] == 4
    assert s["n_censored"] == 0 and s["n_refused"] == 0 and s["by_reason"] == {}
    assert s["complete"] is True and s["settle_s"]["max_is_lower_bound"] is False
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
    s = run.summary
    assert s["non_priming_ok"] == 3 and s["n_refused"] == 1
    assert s["by_reason"] == {"hop_invalid": 1} and s["complete"] is False
    # A refused hop may have settled later than any measured one.
    assert s["settle_s"]["n"] == 3 and s["settle_s"]["max_is_lower_bound"] is True
    assert s["phase_settle_s"]["max_is_lower_bound"] is True
    json.dumps(run.as_dict(), allow_nan=False)


def test_censored_hops_are_counted_not_averaged(tmp_path):
    # Hops 1 and 3 settle inside the first 2 x window samples: censored, excluded from stats.
    taus = {**TAUS, 1: 0.1e-6, 3: 0.1e-6}
    _c, out = _bundle(tmp_path, taus=taus)
    run = analyse_run(out, REF_HZ, tol_hz=TOL)
    assert [h.result.settled_at_first_sample for h in run.hops[1:]] == [True, False, True, False]
    s = run.summary
    assert s["n_censored"] == 2 and s["n_measured"] == 2 and s["n_refused"] == 0
    want = sorted(analytic(DF0, TAUS[i])[0] for i in (2, 4))
    assert s["settle_s"]["n"] == 2
    assert s["settle_s"]["median"] == pytest.approx(sum(want) / 2, abs=2e-6)
    assert s["by_step_hz"][str(30 * MHZ)]["settle_s"] is None  # hop 1 was censored
    assert s["by_step_hz"][str(30 * MHZ)]["n_censored"] == 1


def test_conjugate_flag_recovers_a_swapped_spectrum(tmp_path):
    _c, out = _bundle(tmp_path, conj=True)
    plain = analyse_run(out, REF_HZ, tol_hz=TOL)
    assert not any(h.result.ok for h in plain.hops)
    assert all("mirrored" in h.result.reason for h in plain.hops)
    flipped = analyse_run(out, REF_HZ, tol_hz=TOL, conjugate=True)
    assert all(h.result.ok for h in flipped.hops)
    assert flipped.hops[1].result.settle_s == pytest.approx(analytic(DF0, TAUS[1])[0], abs=2e-6)


def _edit_manifest(out, fn):
    path = out / "manifest.json"
    m = json.loads(path.read_text())
    fn(m)
    path.write_text(json.dumps(m))


def test_context_frequency_mismatch_is_refused(tmp_path):
    _c, out = _bundle(tmp_path)
    _edit_manifest(out, lambda m: m["hops"][2].update(context_hz=m["hops"][2]["planned_hz"] + 1))
    r = analyse_run(out, REF_HZ, tol_hz=TOL).hops[2].result
    assert not r.ok and r.reason.startswith("context_frequency")


def test_missing_hop_file_is_refused(tmp_path):
    _c, out = _bundle(tmp_path)
    (out / "hops" / "hop_0003.npy").unlink()
    run = analyse_run(out, REF_HZ, tol_hz=TOL)
    assert not run.hops[3].result.ok and run.hops[3].result.reason == "missing hop_0003.npy"
    assert run.summary["by_reason"] == {"missing hop_0003.npy": 1}


def test_sample_count_mismatch_is_refused(tmp_path):
    _c, out = _bundle(tmp_path)
    path = out / "hops" / "hop_0004.npy"
    np.save(path, np.load(path)[:-360])
    r = analyse_run(out, REF_HZ, tol_hz=TOL).hops[4].result
    assert not r.ok and "manifest says" in r.reason


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


def test_cli_settle_exits_nonzero_and_warns_when_a_hop_is_refused(tmp_path):
    _c, out = _bundle(tmp_path, drop_context_for={2})
    r = run_cli("settle", str(out), "--ref-hz", str(REF_HZ))
    assert r.returncode == 1
    assert "WARNING: 1 of 4 non-priming hops refused (hop_invalid x1)" in r.stdout
    assert "(lower bound)" in r.stdout
    r = run_cli("settle", str(out), "--ref-hz", str(REF_HZ), "--json")
    assert r.returncode == 1 and "WARNING" in r.stderr
    assert json.loads(r.stdout)["summary"]["n_refused"] == 1


def test_cli_settle_reports_censored_hops(tmp_path):
    _c, out = _bundle(tmp_path, taus={**TAUS, 2: 0.1e-6})
    r = run_cli("settle", str(out), "--ref-hz", str(REF_HZ))
    assert r.returncode == 0, r.stdout
    assert "censored 1" in r.stdout and "settled at their first sample" in r.stdout


def test_cli_settle_rejects_a_bad_window(tmp_path):
    _c, out = _bundle(tmp_path)
    r = run_cli("settle", str(out), "--ref-hz", str(REF_HZ), "--window", "0")
    assert r.returncode == 2 and "window" in r.stderr


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
