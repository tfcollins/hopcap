#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Re-measure a gapless lablp run offline from its kept spool (HOPCAP_KEEP_SPOOL=1).

    scripts/reanalyse_gapless.py <run dir with manifest.json + datagrams.spool> [--json out]

Prints per-hop crossing latency and onset (ns after the scheduled Tick tick) and the summary;
the hardware itself is not touched. Uses the manifest's own edges, slots, f_dds and time-base
parameters, so the result is what the session would have recorded with today's analysis.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hopcap import hoptiming  # noqa: E402
from hopcap.spool import iter_records  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--json", type=Path, help="write hop_timing + summary here")
    a = ap.parse_args()
    m = json.loads((a.run_dir / "manifest.json").read_text())
    tb = m["time_base"]
    tick_hz = m["tick"]["time_tick_hz"]
    with (a.run_dir / "datagrams.spool").open("rb") as fp:
        stream = hoptiming.SampleStream.from_records(
            iter_records(fp, strict=False),
            tick_hz=tick_hz,
            ps_per_sample_q32=tb["armed"]["after_arm"]["ps_per_sample_q32"],
            beats_per_pps=tb["beats_per_pps_used"],
        )
    slots = [tuple(s) for s in m["slots"]]
    hops, prev = [], 0
    for e in m["edges"]:
        if e["status"] in ("EXECUTED", "LATE_EXECUTED", "SCHEDULED"):
            hops.append(
                hoptiming.measure_hop(
                    stream,
                    index=e["index"],
                    executed_ticks=e["executed_ticks"],
                    from_slot=prev,
                    to_slot=e["slot"],
                    slots=slots,
                    f_dds_hz=m["f_dds_hz"],
                    tick_hz=tick_hz,
                )
            )
            prev = e["slot"]
    for h in hops:
        on = "  -  " if h.onset_ns is None else f"{h.onset_ns / 1e3:5.2f}"
        lat = "  -  " if h.latency_ns is None else f"{h.latency_ns / 1e3:5.2f}"
        print(
            f"hop {h.index:3d} {h.kind:8s} {h.from_slot}->{h.to_slot} onset {on} us  cross {lat} us"
        )
    summary = hoptiming.summarize(hops)
    print(json.dumps({k: summary[k] for k in ("all", "onset")}, indent=1))
    if a.json:
        a.json.write_text(
            json.dumps({"hop_timing": [h.as_dict() for h in hops], "summary": summary}, indent=1)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
