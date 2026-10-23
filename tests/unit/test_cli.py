# SPDX-License-Identifier: MIT
"""`hopcap plan --dry-run` is offline and deterministic; golden JSON pins the descriptor hex."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "examples/plans/band-2400-2500-4slot.json"
GOLDEN = ROOT / "tests/fixtures/dry-run-band-2400-2500-4slot.json"


def run_cli(*args):
    env = {"PATH": "", "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'third_party/tick/src'}"}
    return subprocess.run(
        [sys.executable, "-m", "hopcap.cli", *args], capture_output=True, text=True, env=env
    )


def test_plan_dry_run_matches_golden():
    r = run_cli(
        "plan", "--plan", str(PLAN), "--t0-ticks", "0", "--tick-hz", "100000000", "--dry-run"
    )
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got == json.loads(GOLDEN.read_text())


def test_plan_dry_run_contains_descriptor_hex_and_slot_words():
    r = run_cli(
        "plan", "--plan", str(PLAN), "--t0-ticks", "123", "--tick-hz", "100000000", "--dry-run"
    )
    got = json.loads(r.stdout)
    assert got["slots"]["words_q44_20"][0] == str(2_450_000_000 << 20)
    assert len(got["edges"]) == len(got["plan"]["frequencies_hz"]) + 1
    e0 = got["edges"][0]
    assert e0["priming"] is True and e0["rise_ticks"] == 123 + got["plan"]["lead_ticks"]
    assert len(e0["rise_descriptor_hex"].split(",")) == 12
    assert got["timing"]["ok"] is True


def test_plan_rejects_bad_plan_with_nonzero_exit():
    bad = {
        "name": "x",
        "frequencies_hz": [2_600_000_000],
        "period_us": 50_000,
        "settle_us": 10,
        "capture_us": 100,
    }
    p = ROOT / "tests/fixtures/_bad_plan.json"
    p.write_text(json.dumps(bad))
    try:
        r = run_cli("plan", "--plan", str(p), "--dry-run")
        assert r.returncode == 2
        assert "band" in r.stderr
    finally:
        p.unlink()
