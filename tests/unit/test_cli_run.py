# SPDX-License-Identifier: MIT
"""`hopcap run --dry-run` resolves plan + endpoints without touching hardware; `hopcap decode`
rebuilds a bundle from a spool + edge results."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synth import stream_for_plan

from hopcap.plan import HopPlan, compile_plan
from hopcap.spool import write_records

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "examples/plans/band-2400-2500-4slot.json"
ENV = {"PATH": "", "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'third_party/tick/src'}"}


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "hopcap.cli", *args], capture_output=True, text=True, env=ENV
    )


def test_run_dry_run_resolves_everything_offline(tmp_path):
    r = cli(
        "run",
        "--plan",
        str(PLAN),
        "--iio-uri",
        "ip:10.0.0.79",
        "--target",
        "root@10.0.0.79",
        "--bind",
        "192.0.2.2:4991",
        "--rx-interface",
        "enp4s0f0np0",
        "--out",
        str(tmp_path / "o"),
        "--settle-us",
        "150",
        "--capture-us",
        "500",
        "--dry-run",
    )
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["plan"]["settle_ticks"] == 15_000 and d["plan"]["capture_ticks"] == 50_000
    assert (
        d["endpoints"]["tick"]["uri"] == "ip:10.0.0.79"
        and d["endpoints"]["hopctrl"]["base"] == "0x84ac0000"
    )
    assert d["endpoints"]["receiver"]["bind"] == "192.0.2.2:4991"
    assert d["framer"]["dest_ip"] == "192.0.2.2" and d["framer"]["dest_port"] == 4991
    assert d["edges"] and d["edges"][0]["priming"] is True


def test_decode_rebuilds_bundle_from_spool_and_edges(tmp_path):
    p = HopPlan(
        frequencies_hz=(2_410_000_000, 2_430_000_000),
        period_ticks=2_000_000,
        settle_ticks=20_000,
        capture_ticks=100_000,
        pulse_ticks=100_000,
        lead_ticks=500_000,
    )
    c = compile_plan(p, t0_ticks=0, tick_hz=100_000_000)
    recs = stream_for_plan(c, fs=245_760_000, samples_per_packet=360)
    spool = tmp_path / "d.spool"
    with spool.open("wb") as fp:
        write_records(fp, recs)
    edges = [
        {
            "index": e.index,
            "slot": e.slot,
            "commits_hz": e.commits_hz,
            "requested_ticks": e.rise_ticks,
            "executed_ticks": e.rise_ticks,
            "status": "EXECUTED",
            "readback_hz": e.commits_hz,
            "stage_late": False,
        }
        for e in c.edges
    ]
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "plan": {
                    "frequencies_hz": list(p.frequencies_hz),
                    "period_ticks": p.period_ticks,
                    "settle_ticks": p.settle_ticks,
                    "capture_ticks": p.capture_ticks,
                    "pulse_ticks": p.pulse_ticks,
                    "lead_ticks": p.lead_ticks,
                },
                "t0_ticks": 0,
                "tick_hz": 100_000_000,
                "sample_rate_hz": 245_760_000,
                "samples_per_packet": 360,
                "epoch_base": 0,
                "edges": edges,
            }
        )
    )
    r = cli("decode", "--spool", str(spool), "--run", str(run_json), "--out", str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr
    man = json.loads((tmp_path / "out/manifest.json").read_text())
    assert man["disposition"] == "ok" and len(man["hops"]) == 3
    r = cli("verify", "run", str(tmp_path / "out"))
    assert r.returncode == 0, r.stdout + r.stderr
