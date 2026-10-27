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
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
