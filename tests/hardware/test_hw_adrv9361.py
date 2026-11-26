# SPDX-License-Identifier: MIT
"""ADRV9361-Z7035 (lablp) hop proof, checked from lablp session evidence.

``scripts/run_lablp_hopcap_hil.sh`` boots lablp N times (stock SD card, hopcap bitstream
loaded at runtime) and runs one gated and one gapless FastLock run per boot; it then runs
this file with ``HOPCAP_ADRV9361_EVIDENCE`` = the session evidence directories. Without it
every test skips, so the suite collects anywhere.

What is proved, per boot:

* gapless: every scheduled hop -- RX-only, TX-only and both -- lands in the vrt49 capture
  inside the image's latency window after its scheduled Tick timestamp
  (``adrv9361_hop_timing.json``: pin register + FastLock synth settle + RX filter delay), the
  old tone dominates right up to the edge (never early), Tick dropped/overflowed nothing, the
  tap lost no sample, and half-way between edges the tone is still the current slot's (no
  hop without an edge);
* gated: every hop's Context carries its slot's RX LO, epochs advance by one, nothing is
  superseded, every window holds the full capture, and its loopback tone is the slot's
  (TX LO - RX LO + f_dds, so the RX and the TX profile both switched).

Across boots: the per-boot mean latency agrees within ``boot_spread_ns`` and sits within
``mean_tolerance_ns`` of the pinned ``mean_ns``.
"""

from __future__ import annotations

import json
import os
import statistics
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
TIMING = json.loads((HERE / "adrv9361_hop_timing.json").read_text())
SESSIONS = [Path(p) for p in os.environ.get("HOPCAP_ADRV9361_EVIDENCE", "").split(",") if p]

pytestmark = pytest.mark.skipif(
    not SESSIONS, reason="set HOPCAP_ADRV9361_EVIDENCE to lablp session evidence directories"
)


def _manifest(session: Path, run: str) -> dict:
    path = session / run / "manifest.json"
    if not path.is_file():
        pytest.fail(f"{session.name}: no {run}/manifest.json (session did not reach the {run} run)")
    return json.loads(path.read_text())


def _ids():
    return [s.name for s in SESSIONS] or ["none"]


@pytest.fixture(params=SESSIONS or [None], ids=_ids())
def session(request):
    return request.param


def test_gapless_every_hop_lands_on_its_scheduled_tick(session):
    m = _manifest(session, "gapless")
    hops = m["hop_timing"]
    assert len(hops) == len(m["edges"]) >= 8, "every edge must be measured"
    assert m["tick"]["late_count_delta"] == 0, "Tick dropped a late hop"
    assert m["tick"]["overflow_count_delta"] == 0, "Tick queue overflowed"
    assert m["counters"]["framer"]["v49_tx_dropped_packets"] == 0, "tap lost samples"
    lo, hi = TIMING["window_ns"]
    margin = TIMING["min_tone_margin_db"]
    bad = [
        (h["index"], h["kind"], h["latency_ns"], h["old_tone_db"], h["new_tone_db"], h["note"])
        for h in hops
        if h["latency_ns"] is None
        or not lo <= h["latency_ns"] <= hi
        or (h["old_tone_db"] or 0) < margin  # the old tone must hold until the edge
        or (h["new_tone_db"] or 0) < margin
    ]
    assert not bad, f"hops outside [{lo}, {hi}] ns after their Tick timestamp: {bad}"
    mean = m["hop_timing_summary"]["all"]["mean_ns"]
    assert abs(mean - TIMING["mean_ns"]) <= TIMING["mean_tolerance_ns"], mean


def test_gapless_rx_and_tx_hops_both_land(session):
    m = _manifest(session, "gapless")
    kinds = {h["kind"] for h in m["hop_timing"]}
    assert {"rx-only", "tx-only", "both"} <= kinds, f"plan must exercise every hop kind: {kinds}"
    summary = m["hop_timing_summary"]
    lo, hi = TIMING["window_ns"]
    for kind in ("rx-only", "tx-only", "both"):
        assert summary[kind]["n"] >= 2
        assert lo <= summary[kind]["min_ns"] and summary[kind]["max_ns"] <= hi, (
            kind,
            summary[kind],
        )


def test_gapless_no_hop_without_an_edge(session):
    m = _manifest(session, "gapless")
    steady = m["steady_tone"]
    assert len(steady) == len(m["edges"])
    off = [s for s in steady if not s["ok"] or (s["margin_db"] or 0) < 10]
    assert not off, f"tone between edges is not the current slot's: {off}"


def test_gated_contexts_and_windows(session):
    m = _manifest(session, "run")
    hops = m["hops"]
    assert len(hops) == len(m["edges"])
    slots = m["fastlock"]["plan"]["slots"]
    for h in hops:
        assert h["complete"], (h["index"], h.get("reasons"))
        assert h["context_hz"] == slots[h["slot"]]["rx_lo_hz"], h["index"]
    epochs = [h["epoch"] for h in hops]
    assert epochs == list(range(epochs[0], epochs[0] + len(epochs)))
    assert m["hopctrl"]["after"]["superseded_count"] == 0
    assert m["counters"]["framer"]["v49_tx_dropped_packets"] == 0


def test_gated_windows_carry_the_slots_rx_and_tx_tone(session):
    m = _manifest(session, "run")
    plan = m["fastlock"]["plan"]
    fs = m["sample_rate_hz"]
    f_dds = (m["fastlock"].get("bank") or {}).get("f_dds_hz") or plan.get("f_dds_hz")
    wrong = []
    for h in m["hops"]:
        x = np.load(session / "run" / "hops" / f"hop_{h['index']:04d}.npy")
        z = x[:, 0].astype(float) + 1j * x[:, 1]
        spec = np.abs(np.fft.fft(z * np.hanning(len(z))))
        tone = np.fft.fftfreq(len(z), 1 / fs)[int(np.argmax(spec))]
        s = plan["slots"][h["slot"]]
        want = s["tx_lo_hz"] - s["rx_lo_hz"] + f_dds
        if abs(tone - want) > 5 * fs / len(z):
            wrong.append((h["index"], h["slot"], round(tone), want))
    assert not wrong, f"window tone != slot tone: {wrong}"


@pytest.mark.skipif(len(SESSIONS) < 2, reason="repeat-run agreement needs two or more boots")
def test_repeat_boots_agree():
    means = [_manifest(s, "gapless")["hop_timing_summary"]["all"]["mean_ns"] for s in SESSIONS]
    spread = max(means) - min(means)
    assert spread <= TIMING["boot_spread_ns"], f"per-boot mean latency {means} spread {spread} ns"
    assert abs(statistics.fmean(means) - TIMING["mean_ns"]) <= TIMING["mean_tolerance_ns"]
