# SPDX-License-Identifier: MIT
"""VRT (sec, frac ps) -> Tick ticks with the measured PPS period; the board-side Tick/framer
alignment against a simulated board; PPS-period and PCS gates."""

from __future__ import annotations

import random
import re
import subprocess
from fractions import Fraction

import pytest

from hopcap.stamps import q32_for_rate
from hopcap.timebase import (
    SINGLE_PAIR_HINT,
    TickAligner,
    TimeBaseError,
    beats_from_fraction,
    check_pcs,
    check_pps_period,
    decode_pcs,
    nominal_drift_ps,
    read_pps_period,
    tick_ticks_to_vrt,
    vrt_to_tick_ticks,
)

FS = 15_360_000
TICK_HZ = 50_000_000
Q = q32_for_rate(FS)


def test_overlay_q32_matches():
    assert Q == 279620266666667  # kernel/dts/zynq-adrv9361z7035-hopcap-overlay.dts


def test_fraction_inverts_exactly_to_the_beat():
    rng = random.Random(7)
    for k in [
        0,
        1,
        2,
        FS - 1,
        FS,
        FS + 15_000,
        *(rng.randrange(0, FS + 15_360) for _ in range(5000)),
    ]:
        frac = (k * Q) >> 32  # how the framer stamps beat k after the PPS edge
        assert beats_from_fraction(frac, Q) == k
    with pytest.raises(TimeBaseError, match="not floor"):
        beats_from_fraction(((5 * Q) >> 32) + 1, Q)


def test_measured_period_not_nominal():
    """+40 ppm AD9361 reference: 15360614 beats per PPS period. Beat k is k/15360614 of the
    second in Tick time; the nominal reading would be 40 us late by the end of the second."""
    measured = FS + 614
    k = measured - 1
    frac = (k * Q) >> 32
    t = vrt_to_tick_ticks(
        1_700_000_000, frac, tick_hz=TICK_HZ, ps_per_sample_q32=Q, beats_per_pps=measured
    )
    assert t == 1_700_000_000 * TICK_HZ + Fraction(k * TICK_HZ, measured)
    assert 1_700_000_000 * TICK_HZ + TICK_HZ - 4 < t < (1_700_000_000 + 1) * TICK_HZ
    nominal = vrt_to_tick_ticks(
        1_700_000_000, frac, tick_hz=TICK_HZ, ps_per_sample_q32=Q, beats_per_pps=FS
    )
    assert (nominal - t) * 1e6 / TICK_HZ == pytest.approx(39.97, abs=0.05)  # us
    assert nominal_drift_ps(k, nominal_sps=FS, beats_per_pps=measured) / 1e6 == pytest.approx(
        39.97, abs=0.05
    )
    # large second counts stay exact (no float)
    assert isinstance(t, Fraction) and round(t) == 85_000_000_049_999_997


def test_round_trip_to_the_beat():
    measured = FS - 333
    rng = random.Random(3)
    for _ in range(2000):
        sec, k = rng.randrange(1, 2**31), rng.randrange(0, measured)
        frac = (k * Q) >> 32
        t = vrt_to_tick_ticks(
            sec, frac, tick_hz=TICK_HZ, ps_per_sample_q32=Q, beats_per_pps=measured
        )
        assert tick_ticks_to_vrt(
            -(-t.numerator // t.denominator),
            tick_hz=TICK_HZ,
            ps_per_sample_q32=Q,
            beats_per_pps=measured,
        ) == (sec, frac)


def test_pps_period_gate_names_the_single_pair_case():
    assert check_pps_period(FS + 500, nominal_sps=FS, margin=15_360)["offset_ppm"] == pytest.approx(
        32.55, abs=0.01
    )
    with pytest.raises(TimeBaseError, match="four RX"):
        check_pps_period(FS // 2, nominal_sps=FS, margin=15_360)
    with pytest.raises(TimeBaseError, match="no PPS period"):
        check_pps_period(0, nominal_sps=FS, margin=15_360)
    with pytest.raises(TimeBaseError) as e:
        check_pps_period(FS + 20_000, nominal_sps=FS, margin=15_360)
    assert SINGLE_PAIR_HINT not in str(e.value)

    class Mm:
        def read32(self, off):
            return {0x60: FS + 7, 0x64: FS, 0x68: 15_360}[off]

    assert read_pps_period(Mm()) == {"beats_per_pps": FS + 7, "nominal_sps": FS, "margin": 15_360}


def test_pcs_resetdone_gate():
    up = 0x0001D80B  # lablp 2026-10-27: 1000 Mb/s full duplex, AN complete, resetdone
    d = check_pcs(up)
    assert d["resetdone"] and d["link_up"] and not d["los"]
    with pytest.raises(TimeBaseError, match="125 MHz"):
        check_pcs(up & ~(1 << 16))
    with pytest.raises(TimeBaseError, match="absent"):
        check_pcs(up | (1 << 18))
    assert decode_pcs(1 << 20)["mac_txfifo_overflow_sticky"]


class SimBoard:
    """Tick and framer counters driven by one simulated PPS; executes the aligner's commands."""

    def __init__(self, *, straddle: bool = False, queue_level: int = 0):
        self.seconds = 1_700_000_005
        self.tick = 123_456_789
        self.epoch = 0
        self.aligned = 0
        self.armed_tick = False
        self.armed_sec = None
        self.queue_level = queue_level
        self.straddle = straddle
        self.cmds = []

    def edge(self):
        self.seconds = self.armed_sec if self.armed_sec is not None else self.seconds + 1
        self.armed_sec = None
        if self.armed_tick:
            self.tick, self.aligned, self.armed_tick = self.epoch, 1, False
        else:
            self.tick += TICK_HZ - (self.tick % TICK_HZ) if self.aligned else TICK_HZ

    def run(self, cmd, *, timeout_s=15.0):
        self.cmds.append(cmd)
        if "ARMED" in cmd:
            if self.queue_level:
                raise RuntimeError("Tick queue not empty")
            self.edge()  # the poll loop sees the seconds change
            s1 = self.seconds
            n = s1 + 1
            self.epoch = n * int(re.search(r"\$\(\(n \* (\d+)\)\)", cmd).group(1))
            self.armed_tick = True
            if self.straddle:
                self.edge()  # preempted past the next edge before the framer write
            self.armed_sec = n
            return f"ARMED {n} {s1} 412\n"
        if "CHECK" in cmd:
            self.edge()
            self.tick += 1234
            return f"CHECK {self.seconds} {self.tick} {self.seconds} {self.aligned} {self.epoch}\n"
        raise AssertionError(cmd)


def test_aligner_loads_both_on_one_edge():
    b = SimBoard()
    rec = TickAligner(b, TICK_HZ, sleep=lambda s: None).align()
    assert rec["time_aligned"] == 1 and rec["time_epoch_ticks"] == rec["armed_seconds"] * TICK_HZ
    assert rec["check_tick_ticks"] // TICK_HZ == rec["check_seconds"]
    arm = b.cmds[0]
    # the three writes are back to back, after the edge wait, in one board command
    i_wait = arm.index("while")
    i_epoch, i_arm, i_sec = (
        arm.index(x)
        for x in ('time_epoch_ticks"', "arm_time_next_pps", 'echo $n > "$F/v49_tb_seconds"')
    )
    assert i_wait < i_epoch < i_arm < i_sec
    assert "queue_level" in arm
    for cmd in b.cmds:
        assert subprocess.run(["sh", "-n", "-c", cmd]).returncode == 0


def test_aligner_catches_a_straddled_arm():
    with pytest.raises(TimeBaseError, match="different PPS edges"):
        TickAligner(SimBoard(straddle=True), TICK_HZ, sleep=lambda s: None).align()


def test_aligner_retries_a_check_that_straddles_an_edge():
    class Flaky(SimBoard):
        n = 0

        def run(self, cmd, *, timeout_s=15.0):
            out = super().run(cmd, timeout_s=timeout_s)
            if "CHECK" in cmd and self.n == 0:
                self.n = 1
                a, t, b, al, ep = out.split()[1:]
                return f"CHECK {a} {t} {int(b) + 1} {al} {ep}\n"
            return out

    rec = TickAligner(Flaky(), TICK_HZ, sleep=lambda s: None).align()
    assert rec["time_aligned"] == 1
