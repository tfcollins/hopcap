# SPDX-License-Identifier: MIT
"""Hop plan model and compiler.

A :class:`HopPlan` is the operator's intent: an ordered list of frequencies plus one
settle delay and one capture length for the whole plan.  :func:`compile_plan` turns it
into :class:`Edge` records that already honour the Talise FHM one-behind pipeline:

* the engine is one stage deep: GPIO edge *k* switches the RF PLL to the frequency
  software staged before edge *k-1* (validated on tron, see tick/STATUS.md);
* edge 0 therefore always lands on ``fhmInitFrequency`` (2450 MHz by default) and is a
  *priming* edge;
* ``frequencies[k]`` must be staged before edge *k* fires so that edge *k+1* commits it
  (:attr:`Edge.stage_before_hz`).

The hop controller publishes the slot word selected on ``gpio_out[3:1]``; the host
therefore puts, in slot ``edge.slot``, the frequency that is live **after** that edge.
Nothing about the pipeline leaks into RTL.

The ADRV9361 (AD9361 FastLock pin select) has no pipeline: a :class:`FastLockPlan` names up to
eight ``(rx_lo, tx_lo)`` slots and the slot index of every hop; Tick's CTRL_IN code selects RX and
TX profile k together on the edge itself, so there is no priming edge and nothing to stage
(:func:`compile_fastlock_plan`). Its durations are microseconds, converted to Tick ticks at the
platform's Tick rate (50 MHz) when compiled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

Q44_20_SHIFT = 20
MAX_SLOTS = 8


class PlanError(ValueError):
    """A plan that must not be sent to hardware."""


def hz_to_q44_20(hz: int) -> int:
    if hz < 0:
        raise PlanError(f"negative frequency {hz}")
    return hz << Q44_20_SHIFT


def q44_20_to_hz(word: int) -> int:
    if word & ((1 << Q44_20_SHIFT) - 1):
        raise PlanError(f"Q44.20 word 0x{word:x} has a fractional part; plans are integer Hz")
    return word >> Q44_20_SHIFT


@dataclass(frozen=True)
class HopPlan:
    frequencies_hz: tuple[int, ...]
    period_ticks: int
    settle_ticks: int
    capture_ticks: int
    pulse_ticks: int = 1_000_000  # 10 ms at 100 MHz: the width the Tick demo validated
    lead_ticks: int = 5_000_000  # first edge 50 ms after t0 (demo value)
    init_frequency_hz: int = 2_450_000_000  # driver fhmInitFrequency default
    band_hz: tuple[int, int] = (2_400_000_000, 2_500_000_000)
    context_margin_ticks: int = 10_000  # time for the 22-word Context to leave at 10G, generous
    name: str = "hop-plan"

    def capture_samples(self, *, sample_rate_hz: int, tick_hz: int = 100_000_000) -> int:
        return self.capture_ticks * sample_rate_hz // tick_hz

    @staticmethod
    def ticks_for_samples(samples: int, *, sample_rate_hz: int, tick_hz: int = 100_000_000) -> int:
        return -(-samples * tick_hz // sample_rate_hz)  # ceil

    @staticmethod
    def ticks_for_seconds(seconds: float, *, tick_hz: int = 100_000_000) -> int:
        return round(seconds * tick_hz)


@dataclass(frozen=True)
class SlotTable:
    """Up to eight unique frequencies; slot 0 is always the init frequency."""

    frequencies_hz: tuple[int, ...]

    @classmethod
    def from_plan(cls, plan: HopPlan) -> SlotTable:
        ordered: list[int] = [plan.init_frequency_hz]
        for f in plan.frequencies_hz:
            if f not in ordered:
                ordered.append(f)
        if len(ordered) > MAX_SLOTS:
            raise PlanError(
                f"{len(ordered)} unique frequencies (incl. init {plan.init_frequency_hz}) "
                f"exceed the {MAX_SLOTS}-slot table"
            )
        return cls(tuple(ordered))

    def slot_for(self, hz: int) -> int:
        try:
            return self.frequencies_hz.index(hz)
        except ValueError as exc:
            raise PlanError(f"{hz} Hz is not in the slot table") from exc

    def words(self) -> list[int]:
        return [hz_to_q44_20(f) for f in self.frequencies_hz] + [0] * (MAX_SLOTS - len(self))

    def __len__(self) -> int:
        return len(self.frequencies_hz)


@dataclass(frozen=True)
class Edge:
    index: int
    commits_hz: int
    stage_before_hz: int | None  # what the host must have staged before this edge fires
    slot: int
    rise_ticks: int
    fall_ticks: int
    priming: bool


@dataclass(frozen=True)
class CompiledPlan:
    plan: HopPlan
    t0_ticks: int
    tick_hz: int
    slots: SlotTable
    edges: tuple[Edge, ...]
    fastlock: FastLockPlan | None = None  # set for FastLock (ADRV9361) plans

    @property
    def end_ticks(self) -> int:
        return self.edges[-1].rise_ticks + self.plan.settle_ticks + self.plan.capture_ticks


@dataclass
class FhmPipelineModel:
    """Host-side model of what the Talise RF PLL should read back after each edge."""

    init_hz: int
    staged_hz: int = field(init=False)  # most recent frequency_hopping_mode write
    armed_hz: int = field(init=False)  # what the next edge will commit
    expected_hz: int = field(init=False)  # what the PLL should read back now

    def __post_init__(self) -> None:
        self.staged_hz = self.init_hz
        self.armed_hz = self.init_hz
        self.expected_hz = self.init_hz

    def stage(self, hz: int) -> None:
        self.staged_hz = hz

    def on_edge(self, *, executed: bool) -> int:
        """Return the PLL frequency expected after this edge.

        An executed edge commits what was armed (staged before the previous edge)
        and arms the latest staged value.  A dropped edge (Tick ``LATE_DROPPED``)
        moves nothing.
        """
        if executed:
            self.expected_hz = self.armed_hz
            self.armed_hz = self.staged_hz
        return self.expected_hz


def _check_timing(plan: HopPlan, min_period_ticks: int) -> None:
    for name in ("period_ticks", "settle_ticks", "capture_ticks", "pulse_ticks", "lead_ticks"):
        if getattr(plan, name) < 0:
            raise PlanError(f"{name} must be >= 0")
    if plan.pulse_ticks == 0:
        raise PlanError("pulse_ticks must be > 0 so the falling edge exists")
    busy = plan.settle_ticks + plan.capture_ticks + plan.pulse_ticks + plan.context_margin_ticks
    if plan.period_ticks <= busy:
        raise PlanError(
            f"period_ticks {plan.period_ticks} must exceed settle + capture + pulse + context "
            f"margin = {busy}"
        )
    if plan.period_ticks < min_period_ticks:
        raise PlanError(
            f"period_ticks {plan.period_ticks} is below the host staging floor {min_period_ticks} "
            "(hard-timed edge, soft-timed staging: the host cannot stage the next frequency in time)"
        )


def compile_plan(
    plan: HopPlan, *, t0_ticks: int, tick_hz: int, min_period_ticks: int = 0
) -> CompiledPlan:
    """``min_period_ticks`` is the host staging floor: the period must leave room for the
    software ``frequency_hopping_mode`` write (plus readback) between edges."""
    lo, hi = plan.band_hz
    for f in (*plan.frequencies_hz, plan.init_frequency_hz):
        if not lo <= f <= hi:
            raise PlanError(f"{f} Hz outside the FHM band {lo}..{hi} Hz")
    if not plan.frequencies_hz:
        raise PlanError("plan has no frequencies")
    _check_timing(plan, min_period_ticks)
    slots = SlotTable.from_plan(plan)
    edges: list[Edge] = []
    commits = [plan.init_frequency_hz, *plan.frequencies_hz]
    stage_before = [*plan.frequencies_hz, None]
    for k, (commit, stage) in enumerate(zip(commits, stage_before, strict=True)):
        rise = t0_ticks + plan.lead_ticks + k * plan.period_ticks
        edges.append(
            Edge(
                index=k,
                commits_hz=commit,
                stage_before_hz=stage,
                slot=slots.slot_for(commit),
                rise_ticks=rise,
                fall_ticks=rise + plan.pulse_ticks,
                priming=(k == 0),
            )
        )
    return CompiledPlan(
        plan=plan, t0_ticks=t0_ticks, tick_hz=tick_hz, slots=slots, edges=tuple(edges)
    )


# ---------------------------------------------------------------------------
# ADRV9361: FastLock pin-select plans
# ---------------------------------------------------------------------------

# One host round trip (completion poll + ack over network IIO) separates consecutive rising
# edges: Tick's completion window is one entry deep and holds a REPORT_COMPLETION command while
# a completion is unacknowledged, and LATE_DROP then drops it. Network iio_attr calls on lablp
# take ~13 ms but stall in ~1 s steps, so a FastLock plan's period must exceed one stall.
FASTLOCK_MIN_PERIOD_US = 1_000_000
AD9361_LO_RANGE_HZ = (70_000_000, 6_000_000_000)
HOP_KINDS = ("tx", "rx", "both")


@dataclass(frozen=True)
class FastLockSlot:
    rx_lo_hz: int
    tx_lo_hz: int

    def tone_offset_hz(self, f_dds_hz: int) -> int:
        """Where a TX DDS tone at ``f_dds_hz`` lands in RX baseband over the TX1->RX1 coupling."""
        return self.tx_lo_hz - self.rx_lo_hz + f_dds_hz


def hop_kind(a: FastLockSlot, b: FastLockSlot) -> str:
    """``tx`` (only the TX LO moves), ``rx`` (only the RX LO moves), ``both`` or ``none``."""
    rx, tx = a.rx_lo_hz != b.rx_lo_hz, a.tx_lo_hz != b.tx_lo_hz
    return {(False, True): "tx", (True, False): "rx", (True, True): "both"}.get((rx, tx), "none")


@dataclass(frozen=True)
class FastLockPlan:
    """An ADRV9361 hop plan. ``hops[k]`` is the slot (= FastLock profile = CTRL_IN code) live after
    hop k; profile 0 is live before hop 0 (recalled, CTRL_IN parked at 0). Durations in µs."""

    slots: tuple[FastLockSlot, ...]
    hops: tuple[int, ...]
    period_us: float
    settle_us: float
    capture_us: float
    pulse_us: float = 1_000
    lead_us: float = 500_000
    context_margin_us: float = 100
    sample_rate_hz: int = 15_360_000
    f_dds_hz: int = 1_000_000
    band_hz: tuple[int, int] = (2_300_000_000, 2_500_000_000)
    max_offset_fraction: float = 0.4  # tone offsets within +/-(0.4 * fs)
    dc_guard_hz: int = 500_000  # ... and at least this far from DC
    min_step_hz: int = 500_000  # consecutive offsets (and their images) at least this far apart
    min_period_us: float = FASTLOCK_MIN_PERIOD_US
    name: str = "fastlock-plan"

    # -- the loopback tone -----------------------------------------------------------
    def offsets_hz(self) -> tuple[int, ...]:
        return tuple(s.tone_offset_hz(self.f_dds_hz) for s in self.slots)

    def sequence(self) -> tuple[int, ...]:
        """Slot live before each hop and after the last: ``(0, *hops)``."""
        return (0, *self.hops)

    def hop_kinds(self) -> tuple[str, ...]:
        seq = self.sequence()
        return tuple(hop_kind(self.slots[a], self.slots[b]) for a, b in pairwise(seq))

    def validate(self, *, require_all_kinds: bool = False) -> None:
        if not 1 <= len(self.slots) <= MAX_SLOTS:
            raise PlanError(f"{len(self.slots)} slots: a FastLock bank holds 1..{MAX_SLOTS}")
        if len(set(self.slots)) != len(self.slots):
            raise PlanError("two slots carry the same (rx_lo, tx_lo) pair")
        if not self.hops:
            raise PlanError("plan has no hops")
        for k, h in enumerate(self.hops):
            if not 0 <= h < len(self.slots):
                raise PlanError(f"hop {k} names slot {h}; the table has {len(self.slots)} slots")
        lo, hi = self.band_hz
        lo = max(lo, AD9361_LO_RANGE_HZ[0])
        hi = min(hi, AD9361_LO_RANGE_HZ[1])
        for i, s in enumerate(self.slots):
            for what, f in (("rx_lo", s.rx_lo_hz), ("tx_lo", s.tx_lo_hz)):
                if not lo <= f <= hi:
                    raise PlanError(f"slot {i} {what} {f} Hz outside {lo}..{hi} Hz")
        limit = self.max_offset_fraction * self.sample_rate_hz
        for i, off in enumerate(self.offsets_hz()):
            if abs(off) > limit:
                raise PlanError(
                    f"slot {i} tone offset {off} Hz outside +/-{limit:.0f} Hz "
                    f"({self.max_offset_fraction} * fs)"
                )
            if abs(off) < self.dc_guard_hz:
                raise PlanError(f"slot {i} tone offset {off} Hz within {self.dc_guard_hz} Hz of DC")
        offsets = self.offsets_hz()
        seq = self.sequence()
        for k, (a, b) in enumerate(pairwise(seq)):
            oa, ob = offsets[a], offsets[b]
            if abs(oa - ob) < self.min_step_hz:
                raise PlanError(
                    f"hop {k} (slot {a} -> {b}): tone offsets {oa} and {ob} Hz coincide "
                    f"(< {self.min_step_hz} Hz apart): the hop would be invisible in RX"
                )
            if abs(oa + ob) < self.min_step_hz:
                raise PlanError(
                    f"hop {k} (slot {a} -> {b}): new offset {ob} Hz sits on the image of {oa} Hz"
                )
        if require_all_kinds:
            missing = set(HOP_KINDS) - set(self.hop_kinds())
            if missing:
                raise PlanError(f"plan has no {'/'.join(sorted(missing))}-only hop")
        for name in ("period_us", "settle_us", "capture_us", "pulse_us", "lead_us"):
            if getattr(self, name) < 0:
                raise PlanError(f"{name} must be >= 0")
        if self.period_us < self.min_period_us:
            raise PlanError(
                f"period {self.period_us} us below the {self.min_period_us} us floor: each rising "
                "edge's completion must be acknowledged over network IIO (~1 s stalls) before the "
                "next one, or Tick drops it"
            )

    # -- compile ---------------------------------------------------------------------
    def to_hop_plan(self, tick_hz: int) -> HopPlan:
        """The generic, Tick-tick view (what associate/export/the controller read). Its
        ``frequencies_hz`` are the RX LOs: the Context frequency is the slot's RX LO."""
        from hopcap.platform import us_to_ticks

        return HopPlan(
            frequencies_hz=tuple(self.slots[h].rx_lo_hz for h in self.hops),
            period_ticks=us_to_ticks(self.period_us, tick_hz),
            settle_ticks=us_to_ticks(self.settle_us, tick_hz),
            capture_ticks=us_to_ticks(self.capture_us, tick_hz),
            pulse_ticks=us_to_ticks(self.pulse_us, tick_hz),
            lead_ticks=us_to_ticks(self.lead_us, tick_hz),
            init_frequency_hz=self.slots[0].rx_lo_hz,
            band_hz=self.band_hz,
            context_margin_ticks=us_to_ticks(self.context_margin_us, tick_hz),
            name=self.name,
        )

    def slot_table(self) -> SlotTable:
        """Slot words by index (RX LO per profile); duplicates allowed (TX-only slots)."""
        return SlotTable(tuple(s.rx_lo_hz for s in self.slots))

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "slots": [{"rx_lo_hz": s.rx_lo_hz, "tx_lo_hz": s.tx_lo_hz} for s in self.slots],
            "hops": list(self.hops),
            "hop_kinds": list(self.hop_kinds()),
            "tone_offsets_hz": list(self.offsets_hz()),
            "f_dds_hz": self.f_dds_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "period_us": self.period_us,
            "settle_us": self.settle_us,
            "capture_us": self.capture_us,
            "pulse_us": self.pulse_us,
            "lead_us": self.lead_us,
        }

    @classmethod
    def from_dict(cls, raw: dict, *, name: str | None = None) -> FastLockPlan:
        slots = []
        for s in raw["slots"]:
            rx, tx = (s["rx_lo_hz"], s["tx_lo_hz"]) if isinstance(s, dict) else s
            slots.append(FastLockSlot(int(rx), int(tx)))
        kw: dict = {
            "slots": tuple(slots),
            "hops": tuple(int(h) for h in raw["hops"]),
            "name": str(raw.get("name", name or "fastlock-plan")),
        }
        for key in ("period_us", "settle_us", "capture_us"):
            if key not in raw:
                raise PlanError(f"FastLock plan needs {key}")
            kw[key] = float(raw[key])
        for key in ("pulse_us", "lead_us", "context_margin_us", "min_period_us"):
            if key in raw:
                kw[key] = float(raw[key])
        for key in ("sample_rate_hz", "f_dds_hz", "dc_guard_hz", "min_step_hz"):
            if key in raw:
                kw[key] = int(raw[key])
        if "band_hz" in raw:
            kw["band_hz"] = (int(raw["band_hz"][0]), int(raw["band_hz"][1]))
        return cls(**kw)


def is_fastlock_plan(raw: dict) -> bool:
    return "slots" in raw and "hops" in raw


def compile_fastlock_plan(
    plan: FastLockPlan, *, t0_ticks: int, tick_hz: int, min_period_ticks: int = 0
) -> CompiledPlan:
    """Edge k rises at ``t0 + lead + k * period`` on slot ``hops[k]``; it commits that slot's
    RX LO (the Context frequency). No priming edge, nothing staged."""
    plan.validate()
    hp = plan.to_hop_plan(tick_hz)
    _check_timing(hp, min_period_ticks)
    edges = []
    for k, h in enumerate(plan.hops):
        rise = t0_ticks + hp.lead_ticks + k * hp.period_ticks
        edges.append(
            Edge(
                index=k,
                commits_hz=plan.slots[h].rx_lo_hz,
                stage_before_hz=None,
                slot=h,
                rise_ticks=rise,
                fall_ticks=rise + hp.pulse_ticks,
                priming=False,
            )
        )
    return CompiledPlan(
        plan=hp,
        t0_ticks=t0_ticks,
        tick_hz=tick_hz,
        slots=plan.slot_table(),
        edges=tuple(edges),
        fastlock=plan,
    )
