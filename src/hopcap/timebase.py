# SPDX-License-Identifier: MIT
"""VRT time = Tick time on the ADRV9361 (spec decision 3, D1 concerns 2 and 5).

One 1 Hz pulse, ``tick_pps_gen`` on the Tick clock, drives both Tick ``pps_in`` and
``vrt49_time_base_0/pps_in`` (framer PPS source ``"tick"``). Its period is exactly
``TIME_TICK_HZ`` Tick ticks, so once Tick's time is loaded with ``N * TickHz`` on the same edge
that loads the framer's seconds counter with ``N``, PPS edge ``N + j`` is Tick time
``(N + j) * TickHz`` and VRT integer second ``N + j``.

Alignment sequence (:class:`TickAligner`), all on the board in one shell command so no
network round trip sits inside the window:

1. preconditions: the framer time base is LOCKED on ``"tick"`` (``hopcap.framer.arm_time_base``)
   and Tick's queue is empty (its time is about to jump);
2. wait for ``v49_tb_seconds`` to change: a PPS edge has just passed, the next is ~1 s away;
3. write Tick ``time_epoch_ticks = N * TickHz``, Tick ``arm_time_next_pps = 1`` and framer
   ``v49_tb_seconds = N`` (N = the seconds just read + 1), back to back;
4. after the next edge, read framer seconds, Tick ``current_time_ticks``, framer seconds; when
   the two seconds reads agree, require ``ticks // TickHz == seconds`` and ``time_aligned == 1``.

Race window: the three writes in step 3 must all land between the same two PPS edges. They take
microseconds; the window is lost only if the board shell is preempted for the rest of the second
(~1 s) between seeing the edge and the last write. Step 4 catches it: the two counters would then
disagree by one second. The sync latency differs between the two PPS consumers (Tick's two flops
on the Tick clock vs the time base's synchroniser on the AD9361 data clock); that offset is
constant per image and lands in the fixed hop latency the hardware proof pins.

Sub-second time (D1 concern 2): the PPS comes from the PS crystal (FCLK0 -> Tick MMCM), the
sample beats from the AD9361's own reference. The framer stamps the fraction as
``floor(k * q / 2**32)`` ps, ``k`` = beats since the PPS edge and ``q`` the nominal
``ps_per_sample_q32``; at tens of ppm between the two oscillators that drifts against Tick time
by tens of µs by the end of each second. :func:`vrt_to_tick_ticks` therefore inverts the stamp
to ``k`` exactly and rescales it with the MEASURED beats per PPS period (framer register
``TB_PPS_PERIOD``, 0x60, MMIO only), not the nominal rate.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction

TB_PPS_PERIOD = 0x60  # framer: beats between the last two real PPS edges
TB_NOMINAL_SPS = 0x64
TB_PPS_MARGIN = 0x68
PCS_STATUS = 0x78  # adrv9361 SFP framer wrapper: [15:0] PCS status vector, [16] resetdone, ...
PCS_RESETDONE = 1 << 16
PCS_LOS = 1 << 17
PCS_MOD_ABSENT = 1 << 18
PCS_TX_FAULT = 1 << 19
PCS_TXFIFO_OVERFLOW = 1 << 20
PCS_TX_UNDERFLOW = 1 << 21
PCS_LINK_STATUS = 1 << 0  # status vector bit 0: link status
SINGLE_PAIR_HINT = (
    "util_cpack2 packs only enabled channels: with one RX I/Q pair the tap's beat rate halves "
    "(7.68 MHz at 15.36 MSPS) and the tap forwards every other sample. Enable all four RX "
    "converter channels (in_voltage0..3 on cf-ad9361-lpc) and its buffer"
)


class TimeBaseError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# VRT stamp -> Tick ticks
# ---------------------------------------------------------------------------


def beats_from_fraction(frac_ps: int, ps_per_sample_q32: int) -> int:
    """The beat count ``k`` since the PPS edge whose stamp ``floor(k * q / 2**32)`` is
    ``frac_ps``. Unique because one beat is > 1 ps; a stamp off the grid is an error."""
    if frac_ps < 0 or ps_per_sample_q32 <= 0:
        raise TimeBaseError(f"bad stamp fraction {frac_ps} ps / q {ps_per_sample_q32}")
    k = -(-frac_ps * 2**32 // ps_per_sample_q32)
    if (k * ps_per_sample_q32) >> 32 != frac_ps:
        raise TimeBaseError(
            f"fraction {frac_ps} ps is not floor(k * {ps_per_sample_q32} / 2^32) for any k: "
            "the stamp was not made with this ps_per_sample_q32"
        )
    return k


def vrt_to_tick_ticks(
    sec: int,
    frac_ps: int,
    *,
    tick_hz: int,
    ps_per_sample_q32: int,
    beats_per_pps: int,
) -> Fraction:
    """Tick time of a VRT (integer second, fractional ps) stamp, exact.

    ``sec * tick_hz`` is the Tick time of that second's PPS edge (after :class:`TickAligner`);
    the ``k`` beats since the edge last ``k / beats_per_pps`` of a PPS period, which is exactly
    ``tick_hz`` ticks. Pass the measured ``TB_PPS_PERIOD`` as ``beats_per_pps``, not the nominal
    rate. Use ``round()`` for an integer tick."""
    if beats_per_pps <= 0:
        raise TimeBaseError(f"beats_per_pps must be > 0, got {beats_per_pps}")
    k = beats_from_fraction(frac_ps, ps_per_sample_q32)
    return sec * tick_hz + Fraction(k * tick_hz, beats_per_pps)


def tick_ticks_to_vrt(
    ticks: int, *, tick_hz: int, ps_per_sample_q32: int, beats_per_pps: int
) -> tuple[int, int]:
    """Inverse (to the beat): the stamp the framer gives the beat at Tick time ``ticks``."""
    sec, rem = divmod(ticks, tick_hz)
    k = rem * beats_per_pps // tick_hz
    return sec, (k * ps_per_sample_q32) >> 32


def nominal_drift_ps(beats: int, *, nominal_sps: int, beats_per_pps: int) -> float:
    """How far the nominal-rate reading of ``beats`` is from the measured one, in ps."""
    return beats * 1e12 * (1 / nominal_sps - 1 / beats_per_pps)


# ---------------------------------------------------------------------------
# measured PPS period and PCS status (framer MMIO)
# ---------------------------------------------------------------------------


def check_pps_period(beats: int, *, nominal_sps: int, margin: int) -> dict:
    """``TB_PPS_PERIOD`` must sit within the overlay's margin of the nominal beat rate."""
    out = {
        "beats_per_pps": beats,
        "nominal_sps": nominal_sps,
        "margin": margin,
        "offset_ppm": (beats - nominal_sps) * 1e6 / nominal_sps if nominal_sps else None,
    }
    if beats == 0:
        raise TimeBaseError(
            "TB_PPS_PERIOD is 0: no PPS period measured yet (time base not locked, or no "
            f"sample beats at all). {SINGLE_PAIR_HINT}"
        )
    if abs(beats - nominal_sps) > margin:
        hint = SINGLE_PAIR_HINT if abs(beats * 2 - nominal_sps) <= 2 * margin else ""
        raise TimeBaseError(
            f"measured {beats} beats per PPS, nominal {nominal_sps} +/- {margin}. {hint}".strip()
        )
    return out


def read_pps_period(framer_mmio) -> dict:
    return {
        "beats_per_pps": framer_mmio.read32(TB_PPS_PERIOD),
        "nominal_sps": framer_mmio.read32(TB_NOMINAL_SPS),
        "margin": framer_mmio.read32(TB_PPS_MARGIN),
    }


def decode_pcs(word: int) -> dict:
    return {
        "raw": f"0x{word:08x}",
        "status_vector": word & 0xFFFF,
        "link_up": bool(word & PCS_LINK_STATUS),
        "resetdone": bool(word & PCS_RESETDONE),
        "los": bool(word & PCS_LOS),
        "module_absent": bool(word & PCS_MOD_ABSENT),
        "tx_fault": bool(word & PCS_TX_FAULT),
        "mac_txfifo_overflow_sticky": bool(word & PCS_TXFIFO_OVERFLOW),
        "mac_tx_underflow_sticky": bool(word & PCS_TX_UNDERFLOW),
    }


def check_pcs(word: int) -> dict:
    d = decode_pcs(word)
    if not d["resetdone"]:
        raise TimeBaseError(
            f"SFP PCS resetdone (framer 0x78[16]) is 0 ({d['raw']}): the GTX is held in reset. "
            "The AD9517 OUT4 must be 125 MHz and enabled BEFORE the bitstream loads"
        )
    if d["module_absent"]:
        raise TimeBaseError(f"SFP module absent (framer 0x78[18]) ({d['raw']})")
    return d


# ---------------------------------------------------------------------------
# Tick epoch <-> framer seconds on one PPS edge
# ---------------------------------------------------------------------------


def iio_dir_snippet(var: str, name: str) -> str:
    """Board shell: set ``$var`` to the sysfs dir of the IIO device called ``name``."""
    return (
        f"{var}=''; for n in /sys/bus/iio/devices/iio:device*; do "
        f'[ "$(cat $n/name)" = {shlex.quote(name)} ] && {var}=$n; done; '
        f'[ -n "${var}" ] || {{ echo "no IIO device {name}" >&2; exit 3; }}; '
    )


@dataclass
class TickAligner:
    """Load Tick ``N * tick_hz`` and framer seconds ``N`` on the same PPS edge (board side)."""

    shell: object  # hopcap.transport.Shell
    tick_hz: int
    tick_name: str = "axi-timed-command-scheduler"
    framer_name: str = "vrt49_framer"
    edge_timeout_s: float = 3.0
    sleep: Callable[[float], None] = time.sleep
    settle_s: float = 1.2  # one PPS period plus margin before the check
    record: dict = field(default_factory=dict)

    def _prologue(self) -> str:
        return (
            "set -e; "
            + iio_dir_snippet("T", self.tick_name)
            + iio_dir_snippet("F", self.framer_name)
        )

    def arm_command(self) -> str:
        polls = int(self.edge_timeout_s * 1000)
        return self._prologue() + (
            'q=$(cat "$T/queue_level"); [ "$q" = 0 ] || '
            '{ echo "Tick queue not empty ($q)" >&2; exit 5; }; '
            's0=$(cat "$F/v49_tb_seconds"); i=0; '
            'while [ "$(cat "$F/v49_tb_seconds")" = "$s0" ]; do '
            f"i=$((i + 1)); [ $i -le {polls} ] || "
            '{ echo "no PPS edge on the framer time base" >&2; exit 4; }; sleep 0.001; done; '
            's1=$(cat "$F/v49_tb_seconds"); n=$((s1 + 1)); '
            f'echo $((n * {int(self.tick_hz)})) > "$T/time_epoch_ticks"; '
            'echo 1 > "$T/arm_time_next_pps"; '
            'echo $n > "$F/v49_tb_seconds"; '
            'echo "ARMED $n $s1 $i"'
        )

    def check_command(self) -> str:
        return self._prologue() + (
            'a=$(cat "$F/v49_tb_seconds"); t=$(cat "$T/current_time_ticks"); '
            'b=$(cat "$F/v49_tb_seconds"); al=$(cat "$T/time_aligned"); '
            'ep=$(cat "$T/time_epoch_ticks"); echo "CHECK $a $t $b $al $ep"'
        )

    @staticmethod
    def _line(out: str, tag: str) -> list[int]:
        for line in out.splitlines():
            if line.startswith(tag + " "):
                return [int(v, 0) for v in line.split()[1:]]
        raise TimeBaseError(f"no {tag} line from the board: {out.strip()!r}")

    def align(self, *, attempts: int = 3) -> dict:
        n, s1, polls = self._line(self.shell.run(self.arm_command(), timeout_s=30), "ARMED")
        self.record = {"armed_seconds": n, "seconds_at_edge": s1, "edge_polls": polls}
        self.sleep(self.settle_s)
        for _ in range(attempts):
            a, t, b, aligned, epoch = self._line(self.shell.run(self.check_command()), "CHECK")
            if a != b:
                continue  # the reads straddled an edge
            self.record.update(
                {
                    "check_seconds": a,
                    "check_tick_ticks": t,
                    "time_aligned": aligned,
                    "time_epoch_ticks": epoch,
                    "tick_phase_ticks": t % self.tick_hz,
                }
            )
            if epoch != n * self.tick_hz:
                raise TimeBaseError(f"Tick time_epoch_ticks {epoch} != {n} * {self.tick_hz}")
            if aligned != 1:
                raise TimeBaseError("Tick time_aligned is 0: arm_time_next_pps never loaded")
            if a < n:
                raise TimeBaseError(f"framer seconds {a} < armed {n}: the load edge has not passed")
            if t // self.tick_hz != a:
                raise TimeBaseError(
                    f"Tick time {t} = second {t // self.tick_hz} but framer second {a}: the two "
                    "loads landed on different PPS edges (the arm straddled an edge)"
                )
            return self.record
        raise TimeBaseError(f"framer seconds changed during every one of {attempts} checks")
