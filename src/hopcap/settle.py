# SPDX-License-Identifier: MIT
"""Measure Talise hop settle from a fixed (non-hopping) RF reference tone.

Method (datax_sdk_sdr#9): run a plan with ``settle_us: 0`` while a CW reference at ``ref_hz`` is
present at the RX input.  After each hop the reference appears at the complex-baseband offset
``ref_hz - lo_hz`` and the LO's settling shows up as a frequency and phase transient of that tone.

Time zero is the **first captured sample** of the hop, not the Tick edge.  The hop controller
opens the gate only after DRAIN, SETTLE (``settle_ticks``) and PUBLISH (the Context packet left
the framer), and no register records that latency; Tick and VRT time are not mapped onto each
other either.  ``settle_ticks / tick_hz`` is therefore only a lower bound on the open-after-edge
delay, and a hop that is already settled at its first sample (``settled_at_first_sample``) bounds
the settle from above by an unmeasured latency rather than measuring it.

Everything here fails closed: a tone that is out of band, too close to DC or below the SNR
threshold, a frequency estimate noisier than the tolerance allows, or a tail that is still moving
gives ``ok=False`` with a reason, never a number.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

DEFAULT_WINDOW = 256  # ~1.04 us at 245.76 MSPS


def as_complex(iq: np.ndarray) -> np.ndarray:
    """(N, 2) I/Q pairs (the ``hops/hop_NNNN.npy`` layout) or a complex vector -> complex128."""
    a = np.asarray(iq)
    if np.iscomplexobj(a):
        return a.astype(np.complex128, copy=False).reshape(-1)
    if a.ndim != 2 or a.shape[1] != 2:
        raise ValueError(f"expected (N, 2) I/Q pairs or a complex vector, got shape {a.shape}")
    return a[:, 0].astype(np.float64) + 1j * a[:, 1].astype(np.float64)


def _boxcar(x: np.ndarray, window: int) -> np.ndarray:
    """Centred moving sum, same length as ``x``; near the ends it sums the samples that exist."""
    n = len(x)
    c = np.concatenate(([0], np.cumsum(x)))
    lo = np.clip(np.arange(n) - window // 2, 0, n)
    hi = np.clip(lo + window, 0, n)
    return c[hi] - c[lo]


def _mix(x: np.ndarray, fs: float, offset_hz: float) -> np.ndarray:
    """Shift a tone at ``offset_hz`` to 0 Hz.  Phase is reduced mod 1 cycle before exp() so
    long captures keep full precision."""
    cycles = np.mod(offset_hz / fs * np.arange(len(x)), 1.0)
    return x * np.exp(-2j * np.pi * cycles)


def instantaneous_frequency(
    iq: np.ndarray, fs: float, window: int = DEFAULT_WINDOW, offset_hz: float = 0.0
) -> np.ndarray:
    """Instantaneous frequency (Hz, baseband) of the tone near ``offset_hz``, one value per sample.

    The tone is mixed from ``offset_hz`` to 0 Hz and low-passed with a ``window``-sample boxcar
    (first null at fs/window), then the lag-1 phase difference is averaged over another
    ``window`` samples.  Time resolution is about ``window / fs``; the first and last ``window``
    samples average fewer points and are noisier.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    x = as_complex(iq)
    if len(x) < 2:
        raise ValueError("need at least two samples")
    y = _boxcar(_mix(x, fs, offset_hz), window)
    d = _boxcar(y[1:] * np.conj(y[:-1]), window)
    f = np.angle(d) * fs / (2 * np.pi) + offset_hz
    return np.concatenate((f[:1], f))


def _settle_index(dev: np.ndarray, tol: float) -> int | None:
    """First index after which every |dev| <= tol, or None if the last value is outside."""
    bad = np.flatnonzero(np.abs(dev) > tol)
    if len(bad) == 0:
        return 0
    k = int(bad[-1]) + 1
    return k if k < len(dev) else None


@dataclass(frozen=True)
class ToneDetection:
    found_hz: float
    snr_db: float  # tone power over total noise power in the full fs bandwidth (per-sample SNR)


def detect_tone(
    iq: np.ndarray, fs: float, expected_offset_hz: float, search_hz: float
) -> ToneDetection:
    """Strongest Hann-windowed FFT bin within +/-``search_hz`` of the expected offset.

    SNR: tone power (peak +/-3 bins) over total noise power, the noise floor being the median bin
    (median of an exponential = ln 2 x mean).  A noise-only input gives a strongly negative SNR.
    """
    x = as_complex(iq)
    n = len(x)
    spec = np.abs(np.fft.fft(x * np.hanning(n))) ** 2
    freqs = np.fft.fftfreq(n, 1.0 / fs)
    half = max(search_hz, 2 * fs / n)
    sel = np.flatnonzero(np.abs(freqs - expected_offset_hz) <= half)
    if len(sel) == 0:
        return ToneDetection(found_hz=float("nan"), snr_db=-math.inf)
    k = int(sel[np.argmax(spec[sel])])
    idx = np.arange(k - 3, k + 4) % n
    noise_bin = float(np.median(spec)) / math.log(2)
    if noise_bin <= 0:
        return ToneDetection(found_hz=float(freqs[k]), snr_db=math.inf)
    tone = float(spec[idx].sum()) - 7 * noise_bin
    if tone <= 0:
        return ToneDetection(found_hz=float(freqs[k]), snr_db=-math.inf)
    return ToneDetection(found_hz=float(freqs[k]), snr_db=10 * math.log10(tone / (noise_bin * n)))


@dataclass
class SettleResult:
    ok: bool
    reason: str | None
    samples: int
    sample_rate_hz: float
    expected_offset_hz: float
    tol_hz: float
    phase_tol_deg: float
    window: int
    resolution_s: float  # ~window / fs: the time resolution of the estimate
    tone_snr_db: float | None = None
    found_offset_hz: float | None = None  # FFT peak of the tail
    final_offset_hz: float | None = None  # median instantaneous frequency over the tail
    offset_error_hz: float | None = None  # final - expected
    tail_freq_std_hz: float | None = None  # robust std of the estimate over the tail
    settle_s: float | None = None  # from the first captured sample
    phase_settle_s: float | None = None
    settled_at_first_sample: bool | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def settle_time(
    iq: np.ndarray,
    fs: float,
    expected_offset_hz: float,
    tol_hz: float = 1000.0,
    tail_fraction: float = 0.25,
    *,
    phase_tol_deg: float = 5.0,
    window: int = DEFAULT_WINDOW,
    min_snr_db: float = 10.0,
    search_hz: float = 200e3,
    band_fraction: float = 0.8,
    dc_guard_hz: float = 1e6,
    max_noise_fraction: float = 0.2,
) -> SettleResult:
    """Settle of the reference tone in one hop's capture, times from its first sample.

    ``settle_s``: first time after which the smoothed frequency stays within ``tol_hz`` of the
    final value (median over the last ``tail_fraction``).  ``phase_settle_s``: the same on the
    unwrapped phase residual against a straight-line fit of the tail, with ``phase_tol_deg``.
    The last ``window`` samples are not scanned (edge effect of the smoothing).

    Not ok (with ``reason``) when: the expected offset is outside +/-``band_fraction`` x fs/2 or
    within ``dc_guard_hz`` of DC (LO leakage, DC correction and the I/Q image live there); the
    tone's SNR in the tail is below ``min_snr_db`` (with a hint if it sits at the mirrored
    offset, i.e. I/Q swapped); the tail's frequency noise exceeds ``max_noise_fraction`` x
    ``tol_hz``; the tail is still moving; or the frequency never stays within tolerance.
    """
    x = as_complex(iq)
    n = len(x)
    res = SettleResult(
        ok=False,
        reason=None,
        samples=n,
        sample_rate_hz=float(fs),
        expected_offset_hz=float(expected_offset_hz),
        tol_hz=float(tol_hz),
        phase_tol_deg=float(phase_tol_deg),
        window=int(window),
        resolution_s=window / fs,
    )
    if not 0 < tail_fraction < 1:
        raise ValueError("tail_fraction must be in (0, 1)")
    edge = abs(expected_offset_hz)
    if edge > band_fraction * fs / 2:
        res.reason = (
            f"tone_out_of_band: expected offset {expected_offset_hz / 1e6:+.3f} MHz is outside "
            f"+/-{band_fraction * fs / 2e6:.3f} MHz"
        )
        return res
    if edge < dc_guard_hz:
        res.reason = (
            f"tone_near_dc: expected offset {expected_offset_hz / 1e6:+.3f} MHz is within "
            f"{dc_guard_hz / 1e6:.3f} MHz of DC (LO leakage / DC correction); move the reference"
        )
        return res
    tail_n = int(n * tail_fraction)
    if n < 8 * window or tail_n < 4 * window:
        res.reason = f"too_few_samples: {n} samples for window {window}"
        return res

    scan_end = n - window  # drop the noisy smoothing edge at the end
    tail = slice(scan_end - tail_n, scan_end)
    det = detect_tone(x[tail], fs, expected_offset_hz, search_hz)
    res.tone_snr_db = det.snr_db if math.isfinite(det.snr_db) else None
    res.found_offset_hz = det.found_hz if math.isfinite(det.found_hz) else None
    if not det.snr_db >= min_snr_db:
        mirror = detect_tone(x[tail], fs, -expected_offset_hz, search_hz)
        hint = ""
        if mirror.snr_db >= min_snr_db:
            hint = (
                f"; a tone with SNR {mirror.snr_db:.1f} dB sits at the mirrored offset "
                f"{mirror.found_hz / 1e6:+.3f} MHz: I/Q may be swapped (try conjugate)"
            )
        res.reason = (
            f"tone_not_detected: SNR {det.snr_db:.1f} dB < {min_snr_db:.1f} dB within "
            f"+/-{search_hz / 1e3:.0f} kHz of {expected_offset_hz / 1e6:+.3f} MHz{hint}"
        )
        return res

    f = instantaneous_frequency(x, fs, window, expected_offset_hz)
    ft = f[tail]
    final = float(np.median(ft))
    res.final_offset_hz = final
    res.offset_error_hz = final - expected_offset_hz
    noise = float(1.4826 * np.median(np.abs(ft - final)))
    res.tail_freq_std_hz = noise
    if noise > max_noise_fraction * tol_hz:
        res.reason = (
            f"estimate_too_noisy: tail frequency std {noise:.0f} Hz > "
            f"{max_noise_fraction:.2f} x tol {tol_hz:.0f} Hz; raise window or SNR, or widen tol"
        )
        return res
    half = len(ft) // 2
    drift = float(np.median(ft[half:]) - np.median(ft[:half]))
    if abs(drift) > tol_hz / 2:
        res.reason = (
            f"tail_not_settled: frequency moves {drift:+.0f} Hz across the tail; capture longer"
        )
        return res
    k = _settle_index(f[:scan_end] - final, tol_hz)
    if k is None:
        res.reason = "never_settled: frequency is outside tolerance at the end of the capture"
        return res
    res.settle_s = k / fs
    res.settled_at_first_sample = k <= window

    t = np.arange(n) / fs
    y = _boxcar(_mix(x, fs, final), window)
    phi = np.unwrap(np.angle(y))
    slope, icept = np.polyfit(t[tail], phi[tail], 1)
    resid = phi - (slope * t + icept)
    kp = _settle_index(resid[:scan_end], math.radians(phase_tol_deg))
    if kp is None:
        res.reason = "phase_never_settled: phase residual is outside tolerance at the end"
        return res
    res.phase_settle_s = kp / fs
    res.ok = True
    return res


# ---------------------------------------------------------------------------------------------
# Run directory analysis


@dataclass
class HopSettle:
    run: str
    index: int
    priming: bool
    planned_hz: int
    step_hz: int | None  # LO step into this hop (None for the priming hop: prior LO unknown)
    disposition: str
    first_sample_ps: int | None
    open_after_edge_min_s: float  # settle_ticks / tick_hz: lower bound, see module docstring
    result: SettleResult

    def as_dict(self) -> dict:
        d = asdict(self)
        d["result"] = self.result.as_dict()
        return d


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    a = np.asarray(values, dtype=float)
    return {"n": len(a), "median": float(np.median(a)), "max": float(np.max(a))}


def summarise(hops: list[HopSettle]) -> dict:
    """Median and max over the non-priming ok hops, per LO step size, and the priming hops apart."""
    good = [h for h in hops if h.result.ok and not h.priming]
    by_step: dict[str, dict] = {}
    for step in sorted({abs(h.step_hz) for h in good if h.step_hz is not None}):
        sel = [h for h in good if h.step_hz is not None and abs(h.step_hz) == step]
        by_step[str(step)] = {
            "settle_s": _stats([h.result.settle_s for h in sel]),
            "phase_settle_s": _stats([h.result.phase_settle_s for h in sel]),
        }
    prim = [h for h in hops if h.priming]
    return {
        "hops_total": len(hops),
        "hops_ok": sum(h.result.ok for h in hops),
        "non_priming_ok": len(good),
        "settle_s": _stats([h.result.settle_s for h in good]),
        "phase_settle_s": _stats([h.result.phase_settle_s for h in good]),
        "offset_error_hz": _stats([abs(h.result.offset_error_hz) for h in good]),
        "settled_at_first_sample": sum(bool(h.result.settled_at_first_sample) for h in good),
        "by_step_hz": by_step,
        "priming": [
            {
                "run": h.run,
                "index": h.index,
                "ok": h.result.ok,
                "reason": h.result.reason,
                "settle_s": h.result.settle_s,
                "phase_settle_s": h.result.phase_settle_s,
            }
            for h in prim
        ],
        "time_reference": "first captured sample of each hop; open-after-edge latency "
        "(drain + Context publish) is not measured, settle_ticks/tick_hz is its lower bound",
    }


@dataclass
class RunSettle:
    ref_hz: float
    hops: list[HopSettle] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "schema": "hopcap-settle/v1",
            "ref_hz": self.ref_hz,
            "summary": self.summary,
            "hops": [h.as_dict() for h in self.hops],
        }


def analyse_hops(run_dir: Path, ref_hz: float, *, conjugate: bool = False, **kw) -> list[HopSettle]:
    """Per-hop :func:`settle_time` for one hopcap bundle (``manifest.json`` + ``hops/*.npy``)."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    fs = float(manifest["sample_rate_hz"])
    plan = manifest["plan"]
    open_min = plan["settle_ticks"] / plan["tick_hz"]
    out: list[HopSettle] = []
    prev_hz: int | None = None
    for hop in manifest["hops"]:
        lo = int(hop["planned_hz"])
        expected = float(ref_hz) - lo
        step = None if hop["priming"] or prev_hz is None else lo - prev_hz
        prev_hz = lo
        base = SettleResult(
            ok=False,
            reason=None,
            samples=int(hop["samples"]),
            sample_rate_hz=fs,
            expected_offset_hz=expected,
            tol_hz=float(kw.get("tol_hz", 1000.0)),
            phase_tol_deg=float(kw.get("phase_tol_deg", 5.0)),
            window=int(kw.get("window", DEFAULT_WINDOW)),
            resolution_s=int(kw.get("window", DEFAULT_WINDOW)) / fs,
        )
        path = run_dir / "hops" / f"hop_{hop['index']:04d}.npy"
        if hop["disposition"] != "ok":
            base.reason = f"hop_{hop['disposition']}: {', '.join(hop['reasons']) or 'no reason'}"
            res = base
        elif hop.get("context_hz") is not None and int(hop["context_hz"]) != lo:
            base.reason = f"context_frequency {hop['context_hz']} != planned {lo}"
            res = base
        elif not path.exists():
            base.reason = f"missing {path.name}"
            res = base
        else:
            arr = np.load(path, allow_pickle=False)
            if len(arr) != int(hop["samples"]):
                base.reason = f"{path.name} has {len(arr)} samples, manifest says {hop['samples']}"
                res = base
            else:
                x = as_complex(arr)
                if conjugate:
                    x = np.conj(x)
                res = settle_time(x, fs, expected, **kw)
        out.append(
            HopSettle(
                run=str(run_dir),
                index=int(hop["index"]),
                priming=bool(hop["priming"]),
                planned_hz=lo,
                step_hz=step,
                disposition=str(hop["disposition"]),
                first_sample_ps=hop.get("first_sample_ps"),
                open_after_edge_min_s=open_min,
                result=res,
            )
        )
    return out


def analyse_run(
    run_dir: Path | list[Path], ref_hz: float, *, conjugate: bool = False, **kw
) -> RunSettle:
    """Analyse one bundle (or several runs of the same plan) against a reference at ``ref_hz``.

    Keyword arguments go to :func:`settle_time` (``tol_hz``, ``phase_tol_deg``, ``window``,
    ``tail_fraction``, ``min_snr_db``, ...).  ``conjugate`` flips the spectrum (I/Q swap).
    """
    dirs = list(run_dir) if isinstance(run_dir, (list, tuple)) else [run_dir]
    hops: list[HopSettle] = []
    for d in dirs:
        hops += analyse_hops(Path(d), ref_hz, conjugate=conjugate, **kw)
    return RunSettle(ref_hz=float(ref_hz), hops=hops, summary=summarise(hops))
