# SPDX-License-Identifier: MIT
"""run_live on the ADRV9361 platform, end to end with fakes: 50 MHz Tick model, FastLock bank
over a fake AD9361, HOP2 fake, framer + time base with PPS source "tick", Tick alignment hook."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes import FakeMmio, FakeSysfs, FakeTickDevice, FakeTimeBase
from synth import stream_for_plan
from test_fastlock import FakeAd9361, FakeDebugfsShell

from hopcap.fastlock import REG_RX_FASTLOCK_SETUP, REG_TX_FASTLOCK_SETUP, FastLockBank, PhyRegs
from hopcap.framer import FramerConfig
from hopcap.hopctrl import HopCtrlRegs
from hopcap.plan import FastLockPlan, FastLockSlot
from hopcap.platform import ADRV9361, PlatformError
from hopcap.run import Components, RunConfig, run_live
from hopcap.spool import write_records

MHZ = 1_000_000
FS = 15_360_000
Q32 = 279620266666667
SOURCE = ("192.168.2.211", 4991)


class FakeReceiver:
    def __init__(self, spool_path: Path):
        self.spool_path = spool_path
        self.compiled = None
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        recs = stream_for_plan(
            self.compiled, fs=FS, samples_per_packet=249, epoch_base=0, peer=SOURCE
        )
        with self.spool_path.open("wb") as fp:
            write_records(fp, recs)
        return {"spool_records": len(recs), "malformed": 0, "rcvbuf": 1, "peers": []}


def plan() -> FastLockPlan:
    return FastLockPlan(
        slots=(
            FastLockSlot(2_400 * MHZ, 2_400 * MHZ),
            FastLockSlot(2_400 * MHZ, 2_402 * MHZ),
            FastLockSlot(2_405 * MHZ, 2_402 * MHZ),
        ),
        hops=(1, 2, 0),
        period_us=1_250_000,
        settle_us=20,
        capture_us=2_000,
        lead_us=100_000,
    )


def setup(tmp_path, *, tick_hz=50_000_000):
    regs = HopCtrlRegs.from_json()
    mmio = FakeMmio(regs)
    tick = FakeTickDevice(step=500_000, tick_hz=tick_hz)
    framer = FakeSysfs(
        time_base=FakeTimeBase(names=("tick", "disabled"), nominal_sps=FS, ps_per_sample_q32=Q32)
    )
    chip = FakeAd9361()
    order: list[str] = []
    bank = FastLockBank(
        write_channel=chip.write_channel,
        write_debug=chip.write_debug,
        regs=PhyRegs(FakeDebugfsShell(chip)),
        log=lambda m: order.append("bank: " + m),
    )
    rx = FakeReceiver(tmp_path / "out" / "datagrams.spool")
    real_write = tick.write_attr

    def tick_write(name, value):
        if name == "command_descriptor":
            words = [int(w, 16) for w in value.split(",")]
            order.append(f"tick gpio mask=0x{words[6]:x} value=0x{words[7]:x}")
        real_write(name, value)

    def on_compiled(c):
        rx.compiled = c
        mmio.mem[regs.EPOCH] = len(c.edges)

    def time_align():
        order.append("time_align")
        return {"tick_alignment": {"time_aligned": 1}}

    comps = Components(
        tick_read=tick.read_attr,
        tick_write=tick_write,
        mmio=mmio,
        framer=framer,
        stager=bank,
        receiver=rx,
        pipeline=None,
        regs=regs,
        on_compiled=on_compiled,
        sleep=lambda s: None,
        monotonic=framer.tb.clock,
        log=lambda m: None,
        time_align=time_align,
        pps_period=lambda: {"beats_per_pps": FS + 100, "nominal_sps": FS, "margin": 15_360},
    )
    cfg = RunConfig(
        plan=plan(),
        out_dir=tmp_path / "out",
        framer=FramerConfig.for_platform(ADRV9361),
        expected_peer=SOURCE,
        platform=ADRV9361,
        lookahead_hops=7,
    )
    return cfg, comps, mmio, regs, chip, tick, order


def test_adrv9361_run_live(tmp_path):
    cfg, comps, mmio, regs, chip, _tick, order = setup(tmp_path)
    report = run_live(cfg, comps)
    assert report.disposition == "ok"
    m = report.manifest
    assert m["platform"] == "adrv9361" and m["tick"]["time_tick_hz"] == 50_000_000
    # each edge commits its own slot's RX LO: no Talise pipeline, no priming edge
    assert [e["commits_hz"] for e in m["edges"]] == [2_400 * MHZ, 2_405 * MHZ, 2_400 * MHZ]
    assert [e["slot"] for e in m["edges"]] == [1, 2, 0]
    assert all(e["readback_hz"] is None for e in m["edges"])
    assert [h["disposition"] for h in m["hops"]] == ["ok"] * 3
    # controller registers in 100 MHz cycles, plan in 50 MHz ticks
    w = dict(mmio.writes)
    assert w[regs.SETTLE_TICKS] == 2_000 and w[regs.CAPTURE_TICKS] == 200_000
    assert w[regs.slot_lo(1)] == ((2_400 * MHZ) << 20) & 0xFFFFFFFF  # TX-only slot by index
    # rise mask 0xF value 0x8|slot, fall mask 0x8
    gpio = [x for x in order if x.startswith("tick gpio")]
    assert gpio[0] == "tick gpio mask=0xf value=0x0"  # park CTRL_IN at code 0
    assert gpio[1:] == [
        "tick gpio mask=0xf value=0x9",
        "tick gpio mask=0x8 value=0x0",
        "tick gpio mask=0xf value=0xa",
        "tick gpio mask=0x8 value=0x0",
        "tick gpio mask=0xf value=0x8",
        "tick gpio mask=0x8 value=0x0",
    ]
    # order: time base locked + Tick aligned -> profiles stored -> park -> pin select -> hops
    i_align = order.index("time_align")
    i_bank = next(i for i, x in enumerate(order) if x.startswith("bank: fastlock: 3"))
    i_park = order.index("tick gpio mask=0xf value=0x0")
    i_pin = next(i for i, x in enumerate(order) if "pin select on" in x)
    assert i_align < i_bank < i_park < i_pin < order.index("tick gpio mask=0xf value=0x9")
    # pin select released at teardown; nothing wrote an LO after it was set
    assert chip.reg[REG_RX_FASTLOCK_SETUP] == 0x01 and chip.reg[REG_TX_FASTLOCK_SETUP] == 0x01
    assert m["fastlock"]["bank"]["pin_select_on"] == {"0x25A": [1, 3], "0x29A": [1, 3]}
    assert m["fastlock"]["plan"]["hop_kinds"] == ["tx", "rx", "both"]
    tb = m["time_base"]
    assert tb["armed"]["after_arm_hook"] == {"tick_alignment": {"time_aligned": 1}}
    assert tb["pps_period_before"]["beats_per_pps"] == FS + 100
    assert tb["armed"]["after_arm"]["pps_source"] == "tick"
    assert tb["stamps"]["step_errors"] == 0 and tb["stamps"]["steps_checked"] > 0


def test_adrv9361_run_refuses_a_tick_not_at_50_mhz(tmp_path):
    cfg, comps, *_ = setup(tmp_path, tick_hz=100_000_000)
    with pytest.raises(PlatformError, match="expected 50000000"):
        run_live(cfg, comps)


def test_platform_and_plan_must_match(tmp_path):
    from hopcap.platform import ZU11EG

    cfg, comps, *_ = setup(tmp_path)
    cfg.platform = ZU11EG
    with pytest.raises(ValueError, match="talise-fhm"):
        run_live(cfg, comps)


def test_adrv9361_bundle_passes_the_run_verifier(tmp_path):
    """The launcher's last gate: verify_evidence.py run on the adrv9361 bundle."""
    import subprocess

    cfg, comps, *_ = setup(tmp_path)
    run_live(cfg, comps)
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        [sys.executable, str(root / "scripts/verify_evidence.py"), "run", str(tmp_path / "out")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
