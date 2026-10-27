# SPDX-License-Identifier: MIT
"""Write the per-run bundle: SigMF dataset, per-hop npy, raw spool, manifest, checksums."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

import numpy as np

from hopcap import __version__, vrt
from hopcap.associate import AssociationResult
from hopcap.plan import CompiledPlan
from hopcap.spool import Record, write_records

MANIFEST_SCHEMA = "hopcap-manifest/v1"
PS = 10**12


def _iso_from_ps(ps: int) -> str:
    secs, frac = divmod(ps, PS)
    base = _dt.datetime.fromtimestamp(secs, tz=_dt.timezone.utc)
    return base.strftime("%Y-%m-%dT%H:%M:%S") + f".{frac // 10**3:09d}Z"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hop_iq(records_by_index: dict[int, list[bytes]]) -> dict[int, np.ndarray]:
    out: dict[int, np.ndarray] = {}
    for idx, datagrams in records_by_index.items():
        parts = [vrt.parse(d).iq() for d in datagrams]
        out[idx] = np.concatenate(parts) if parts else np.zeros((0, 2), dtype=np.int16)
    return out


def write_bundle(
    out_dir: Path,
    *,
    compiled: CompiledPlan,
    result: AssociationResult,
    records: Iterable[Record],
    sample_rate_hz: int,
    provenance: dict,
    counters: dict,
    clock_domains: dict | None = None,
) -> Path:
    out_dir = Path(out_dir)
    (out_dir / "hops").mkdir(parents=True, exist_ok=True)
    records = list(records)

    # Rebuild each hop's samples from the spool: exactly the IF Data records that fall inside
    # the association's arrival window [first_arrival, last_arrival], never trailing data.
    by_hop: dict[int, list[bytes]] = {h.index: [] for h in result.hops}
    spans = [
        (h.index, h.first_arrival_mono_ns, h.last_arrival_mono_ns)
        for h in result.hops
        if h.first_arrival_mono_ns is not None and h.last_arrival_mono_ns is not None
    ]
    for rec in records:
        try:
            pkt = vrt.parse(rec.data)
        except vrt.VrtError:
            continue
        if isinstance(pkt, vrt.Context):
            continue
        for idx, lo, hi in spans:
            if lo <= rec.mono_ns <= hi:
                by_hop[idx].append(rec.data)
                break
    iq = _hop_iq(by_hop)

    # SigMF: complete + tagged hops only, concatenated in plan order.
    segments: list[np.ndarray] = []
    captures: list[dict] = []
    annotations: list[dict] = []
    start = 0
    for h in result.hops:
        arr = iq.get(h.index, np.zeros((0, 2), dtype=np.int16))
        np.save(out_dir / "hops" / f"hop_{h.index:04d}.npy", arr.astype("<i2", copy=False))
        if h.disposition == "invalid" or h.context_hz is None or h.samples == 0:
            continue
        cap = {"core:sample_start": start, "core:frequency": h.context_hz}
        if h.first_sample_ps is not None:
            cap["core:datetime"] = _iso_from_ps(h.first_sample_ps)
        captures.append(cap)
        annotations.append(
            {
                "core:sample_start": start,
                "core:sample_count": len(arr),
                "core:label": f"hop {h.index} epoch {h.epoch}",
                "hopcap:hop_index": h.index,
                "hopcap:epoch": h.epoch,
                "hopcap:slot": h.slot,
                "hopcap:priming": h.priming,
                "hopcap:tick_executed": h.tick_executed,
                "hopcap:disposition": h.disposition,
                "hopcap:gaps": h.gaps,
            }
        )
        segments.append(arr)
        start += len(arr)
    data = np.concatenate(segments) if segments else np.zeros((0, 2), dtype=np.int16)
    data.astype("<i2", copy=False).tofile(out_dir / "run.sigmf-data")
    meta = {
        "global": {
            "core:datatype": "ci16_le",
            "core:sample_rate": sample_rate_hz,
            "core:version": "1.0.0",
            "core:description": f"hopcap {compiled.plan.name}: Tick-timed Talise FHM hops, hardware-gated captures",
            "core:recorder": f"hopcap {__version__}",
            "core:hw": "ADRV9009-ZU11EG + vrt49_fhm_hop_ctrl",
            "hopcap:manifest": "manifest.json",
        },
        "captures": captures,
        "annotations": annotations,
    }
    (out_dir / "run.sigmf-meta").write_text(json.dumps(meta, indent=2) + "\n")

    with (out_dir / "datagrams.spool").open("wb") as fp:
        write_records(fp, records)

    plan = compiled.plan
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "generated_utc": _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plan": {
            **asdict(plan),
            "frequencies_hz": list(plan.frequencies_hz),
            "band_hz": list(plan.band_hz),
            "t0_ticks": compiled.t0_ticks,
            "tick_hz": compiled.tick_hz,
            "slots_hz": list(compiled.slots.frequencies_hz),
            "capture_samples": plan.capture_samples(
                sample_rate_hz=sample_rate_hz, tick_hz=compiled.tick_hz
            ),
        },
        "sample_rate_hz": sample_rate_hz,
        "provenance": provenance,
        "clock_domains": clock_domains
        or {
            "tick": {
                "description": "axi_timed_command_scheduler ticks, reset-relative unless PPS-aligned",
                "hz": compiled.tick_hz,
                "mapped_to_vrt": False,
            },
            "vrt": {
                "description": "framer timestamp (vrt49_time_base, armed by hopcap), TSI=UTC "
                "TSF=real-time ps: seconds are host UTC at arm (+/-1 s), the sub-second phase is "
                "the selected PPS (Corundum's free-running PTP PPS by default), not GNSS",
                "mapped_to_tick": False,
            },
            "host_monotonic_ns": {"description": "receiver arrival clock (CLOCK_MONOTONIC)"},
            "host_realtime_ns": {"description": "receiver arrival clock (CLOCK_REALTIME)"},
        },
        "counters": counters,
        "counts": asdict(result.counts),
        "epochs_seen": result.epochs_seen,
        "hops": [h.as_dict() for h in result.hops],
        "disposition": result.disposition,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")

    write_sums(out_dir)
    return out_dir


BUNDLE_FILES = ("run.sigmf-data", "run.sigmf-meta", "manifest.json", "datagrams.spool")


def write_sums(out_dir: Path) -> dict[str, str]:
    """SHA256SUMS.json over the bundle's own outputs (hops/*.npy included). Operator files that
    keep growing after the bundle is written (launcher.log, junit) are deliberately not listed."""
    out_dir = Path(out_dir)
    files = [out_dir / n for n in BUNDLE_FILES if (out_dir / n).exists()]
    files += sorted((out_dir / "hops").glob("*.npy"))
    sums = {str(p.relative_to(out_dir)): _sha256(p) for p in files}
    (out_dir / "SHA256SUMS.json").write_text(json.dumps(sums, indent=2) + "\n")
    return sums
