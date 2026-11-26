# SPDX-License-Identifier: MIT
"""ADRV9361 FastLock plans: slot identity by index, tone-offset validation, µs -> 50 MHz ticks,
and the example plans under examples/plans/adrv9361-*.json."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest

from hopcap.cli import load_fastlock_plan, plan_platform
from hopcap.descriptors import descriptors_for_plan
from hopcap.plan import (
    FastLockPlan,
    FastLockSlot,
    PlanError,
    compile_fastlock_plan,
    hop_kind,
)
from hopcap.platform import ADRV9361, ADRV9361_BITS

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = sorted((ROOT / "examples/plans").glob("adrv9361-*.json"))
MHZ = 1_000_000
FS = 15_360_000


def S(rx_mhz: float, tx_mhz: float) -> FastLockSlot:
    return FastLockSlot(int(rx_mhz * MHZ), int(tx_mhz * MHZ))


def plan(**kw) -> FastLockPlan:
    base = dict(
        slots=(S(2400, 2400), S(2400, 2402), S(2405, 2402)),
        hops=(1, 2, 0),
        period_us=1_250_000,
        settle_us=20,
        capture_us=4_000,
    )
    base.update(kw)
    return FastLockPlan(**base)


def test_tone_offset_and_hop_kinds():
    p = plan()
    assert p.offsets_hz() == (1 * MHZ, 3 * MHZ, -2 * MHZ)
    assert p.sequence() == (0, 1, 2, 0)
    assert p.hop_kinds() == ("tx", "rx", "both")
    assert hop_kind(S(2400, 2400), S(2400, 2400)) == "none"
    p.validate(require_all_kinds=True)


def test_tx_only_slots_keep_their_own_index():
    """Two slots share an RX LO (a TX-only hop). SlotTable.from_plan would dedup them and map
    both to the first; the FastLock compile must keep slot = profile = CTRL_IN code by index."""
    c = compile_fastlock_plan(plan(), t0_ticks=0, tick_hz=ADRV9361.tick_hz)
    assert [e.slot for e in c.edges] == [1, 2, 0]
    assert c.slots.frequencies_hz == (2_400 * MHZ, 2_400 * MHZ, 2_405 * MHZ)
    assert c.slots.words()[1] == (2_400 * MHZ) << 20  # duplicate RX LO kept at index 1
    assert [e.commits_hz for e in c.edges] == [2_400 * MHZ, 2_405 * MHZ, 2_400 * MHZ]
    d = descriptors_for_plan(c, ADRV9361_BITS)
    assert [x.args[:2] for x in d] == [
        (0xF, 0x9),
        (0x8, 0),
        (0xF, 0xA),
        (0x8, 0),
        (0xF, 0x8),
        (0x8, 0),
    ]


def test_compile_converts_us_at_50_mhz_with_no_priming_or_staging():
    c = compile_fastlock_plan(plan(), t0_ticks=1_000, tick_hz=50_000_000)
    hp = c.plan
    assert (hp.period_ticks, hp.settle_ticks, hp.capture_ticks) == (62_500_000, 1_000, 200_000)
    assert hp.pulse_ticks == 50_000 and hp.lead_ticks == 25_000_000
    assert c.edges[0].rise_ticks == 1_000 + 25_000_000
    assert c.edges[2].rise_ticks == 1_000 + 25_000_000 + 2 * 62_500_000
    assert all(e.fall_ticks == e.rise_ticks + 50_000 for e in c.edges)
    assert not any(e.priming for e in c.edges)
    assert all(e.stage_before_hz is None for e in c.edges)
    assert c.fastlock is not None and c.tick_hz == 50_000_000
    assert hp.capture_samples(sample_rate_hz=FS, tick_hz=50_000_000) == 61_440


@pytest.mark.parametrize(
    "kw, match",
    [
        (dict(slots=tuple(S(2400, 2400 + k) for k in range(9)), hops=(1,)), "1..8"),
        (dict(slots=(S(2400, 2400), S(2400, 2400)), hops=(1,)), "same"),
        (dict(hops=()), "no hops"),
        (dict(hops=(1, 3)), "names slot 3"),
        # 0 -> 1 with equal offsets: the hop would be invisible
        (dict(slots=(S(2400, 2400), S(2402, 2402)), hops=(1,)), "coincide"),
        # +1 MHz -> -1 MHz: the new tone sits on the old one's image
        (dict(slots=(S(2400, 2400), S(2402, 2400)), hops=(1,)), "image"),
        # +7 MHz is outside +/-0.4 fs (6.144 MHz)
        (dict(slots=(S(2400, 2400), S(2400, 2406)), hops=(1,)), "outside"),
        # tx - rx + f_dds = 0.2 MHz: too close to DC
        (dict(slots=(S(2400, 2400), S(2400.8, 2400)), hops=(1,)), "DC"),
        (dict(slots=(S(2400, 2400), S(2600, 2602)), hops=(1,)), "outside 2300000000"),
        (dict(period_us=300_000), "floor"),
        (dict(capture_us=-1), "capture_us"),
    ],
)
def test_validation_rejects(kw, match):
    with pytest.raises(PlanError, match=match):
        plan(**kw).validate()


def test_require_all_kinds():
    p = plan(hops=(1,))
    p.validate()
    with pytest.raises(PlanError, match="both/rx"):
        p.validate(require_all_kinds=True)


def test_period_must_cover_settle_capture_and_pulse():
    p = plan(period_us=1_000_000, capture_us=999_000, pulse_us=2_000)
    with pytest.raises(PlanError, match="must exceed"):
        compile_fastlock_plan(p, t0_ticks=0, tick_hz=50_000_000)


def test_round_trip_through_json():
    p = plan()
    q = FastLockPlan.from_dict(
        {**p.as_dict(), "slots": [[s.rx_lo_hz, s.tx_lo_hz] for s in p.slots]}
    )
    assert (q.slots, q.hops, q.period_us, q.f_dds_hz) == (p.slots, p.hops, p.period_us, p.f_dds_hz)
    with pytest.raises(PlanError, match="period_us"):
        FastLockPlan.from_dict({"slots": [[1, 1]], "hops": [0]})


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_plans_meet_the_spec(path):
    """15.36 MSPS, LOs near 2.4 GHz, a distinct tone offset on every consecutive hop, every
    offset inside +/-0.4 fs and away from DC, TX-only, RX-only and both hops present."""
    raw = json.loads(path.read_text())
    assert raw["platform"] == "adrv9361" and raw["sample_rate_hz"] == FS
    p = load_fastlock_plan(path)
    p.validate(require_all_kinds=True)
    assert len(p.slots) <= 8
    for s in p.slots:
        assert abs(s.rx_lo_hz - 2_400 * MHZ) < 50 * MHZ and abs(s.tx_lo_hz - 2_400 * MHZ) < 50 * MHZ
    offs = p.offsets_hz()
    assert all(MHZ // 2 <= abs(o) <= 0.4 * FS for o in offs)
    seq = p.sequence()
    assert all(offs[a] != offs[b] for a, b in pairwise(seq))
    assert p.period_us % 1_000_000 != 0, "hops should sample different phases of the PPS second"
    assert plan_platform(path, None) is ADRV9361
    compile_fastlock_plan(p, t0_ticks=0, tick_hz=ADRV9361.tick_hz)


def test_eight_slot_example_uses_every_slot():
    p = load_fastlock_plan(ROOT / "examples/plans/adrv9361-loopback-8slot.json")
    assert len(p.slots) == 8 and set(p.hops) == set(range(8))


def test_platform_and_plan_kind_must_agree():
    with pytest.raises(PlanError, match="Talise FHM plan"):
        plan_platform(ROOT / "examples/plans/band-2400-2500-4slot.json", "adrv9361")
    with pytest.raises(PlanError, match="adrv9361 plan, not zu11eg"):
        plan_platform(EXAMPLES[0], "zu11eg")


def test_cli_dry_run_for_adrv9361():
    env = {"PATH": "", "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'third_party/tick/src'}"}
    r = subprocess.run(
        [sys.executable, "-m", "hopcap.cli", "plan", "--plan", str(EXAMPLES[0]), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["platform"] == "adrv9361" and out["tick_hz"] == 50_000_000
    assert out["controller_cycles"]["capture"] == 2 * out["tick_ticks"]["capture_ticks"]
    for e in out["edges"]:
        assert e["rise_gpio"] == {"mask": 0xF, "value": 0x8 | e["slot"]}
        assert e["fall_gpio"] == {"mask": 0x8, "value": 0}
        assert e["tone_from_hz"] != e["tone_to_hz"] and e["priming"] is False


def test_replace_keeps_validation_live():
    p = replace(plan(), hops=(1, 1))
    with pytest.raises(PlanError, match="coincide"):
        p.validate()
