# SPDX-License-Identifier: MIT
"""hopcap command line.

``hopcap plan --plan FILE --dry-run`` compiles a plan offline and prints JSON: slot table,
edges with their Tick descriptors as the 12 hex words the driver accepts, and the timing
checks.  Live commands (``run``) live in :mod:`hopcap.run` and are wired in later phases.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from tick.ffh import descriptor_csv

from hopcap.descriptors import descriptors_for_edge
from hopcap.plan import HopPlan, PlanError, compile_plan

DEFAULT_TICK_HZ = 100_000_000
DEFAULT_FS_HZ = 245_760_000


def load_plan(path: Path, *, tick_hz: int = DEFAULT_TICK_HZ) -> tuple[HopPlan, int]:
    """Plan JSON uses human units (µs) or samples; everything becomes ticks here."""
    raw = json.loads(Path(path).read_text())
    fs = int(raw.get("sample_rate_hz", DEFAULT_FS_HZ))

    def ticks(key_us: str, key_ticks: str, default: int | None = None) -> int:
        if key_ticks in raw:
            return int(raw[key_ticks])
        if key_us in raw:
            return HopPlan.ticks_for_seconds(float(raw[key_us]) * 1e-6, tick_hz=tick_hz)
        if default is None:
            raise PlanError(f"plan needs {key_us} or {key_ticks}")
        return default

    if "capture_samples" in raw:
        capture = HopPlan.ticks_for_samples(
            int(raw["capture_samples"]), sample_rate_hz=fs, tick_hz=tick_hz
        )
    else:
        capture = ticks("capture_us", "capture_ticks")
    kw = dict(
        frequencies_hz=tuple(int(f) for f in raw["frequencies_hz"]),
        period_ticks=ticks("period_us", "period_ticks"),
        settle_ticks=ticks("settle_us", "settle_ticks"),
        capture_ticks=capture,
        pulse_ticks=ticks("pulse_us", "pulse_ticks", HopPlan.pulse_ticks),
        lead_ticks=ticks("lead_us", "lead_ticks", HopPlan.lead_ticks),
        name=str(raw.get("name", Path(path).stem)),
    )
    if "init_frequency_hz" in raw:
        kw["init_frequency_hz"] = int(raw["init_frequency_hz"])
    if "band_hz" in raw:
        kw["band_hz"] = (int(raw["band_hz"][0]), int(raw["band_hz"][1]))
    return HopPlan(**kw), fs


def dry_run(plan: HopPlan, *, fs: int, t0_ticks: int, tick_hz: int) -> dict:
    c = compile_plan(plan, t0_ticks=t0_ticks, tick_hz=tick_hz)
    edges = []
    for e in c.edges:
        rise, fall = descriptors_for_edge(e)
        edges.append(
            {
                **asdict(e),
                "rise_sequence_id": rise.sequence_id,
                "fall_sequence_id": fall.sequence_id,
                "rise_descriptor_hex": descriptor_csv(rise),
                "fall_descriptor_hex": descriptor_csv(fall),
                "capture_open_ticks": e.rise_ticks + plan.settle_ticks,
                "capture_close_ticks": e.rise_ticks + plan.settle_ticks + plan.capture_ticks,
            }
        )
    busy = plan.settle_ticks + plan.capture_ticks + plan.pulse_ticks + plan.context_margin_ticks
    return {
        "schema": "hopcap-dry-run/v1",
        "plan": {
            **asdict(plan),
            "frequencies_hz": list(plan.frequencies_hz),
            "band_hz": list(plan.band_hz),
        },
        "tick_hz": tick_hz,
        "t0_ticks": t0_ticks,
        "sample_rate_hz": fs,
        "capture_samples": plan.capture_samples(sample_rate_hz=fs, tick_hz=tick_hz),
        "slots": {
            "frequencies_hz": list(c.slots.frequencies_hz),
            "words_q44_20": [str(w) for w in c.slots.words()],
        },
        "edges": edges,
        "timing": {
            "ok": True,
            "busy_ticks": busy,
            "period_ticks": plan.period_ticks,
            "slack_ticks": plan.period_ticks - busy,
            "end_ticks": c.end_ticks,
            "duration_s": (c.end_ticks - t0_ticks) / tick_hz,
        },
        "pipeline_note": "edge k commits the frequency staged before edge k-1; edge 0 primes on init_frequency_hz",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hopcap")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="compile a plan; --dry-run prints JSON and touches no hardware")
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--t0-ticks", type=int, default=0)
    p.add_argument("--tick-hz", type=int, default=DEFAULT_TICK_HZ)
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            plan, fs = load_plan(args.plan, tick_hz=args.tick_hz)
            out = dry_run(plan, fs=fs, t0_ticks=args.t0_ticks, tick_hz=args.tick_hz)
            json.dump(out, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0
    except PlanError as exc:
        print(f"hopcap: plan error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
