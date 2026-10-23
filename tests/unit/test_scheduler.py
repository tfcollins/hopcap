# SPDX-License-Identifier: MIT
"""TickSession against a fake IIO device backed by tick.hdl_model with a virtual clock."""

from __future__ import annotations

from tick import CommandDescriptor, Flag, Status
from tick.hdl_model import AxiTimedCommandSchedulerModel

from hopcap.plan import FhmPipelineModel, HopPlan, compile_plan
from hopcap.scheduler import TickSession

TICK_HZ = 100_000_000
MHZ = 1_000_000


class FakeTickDevice:
    """read_attr/write_attr over the HDL model. Time advances `step` ticks per poll of
    current_time_ticks, so waits terminate without real sleeping."""

    def __init__(self, step: int = 50_000):
        self.model = AxiTimedCommandSchedulerModel(queue_depth=16, tick_hz=TICK_HZ)
        self.now = 0
        self.step = step
        self.pushed: list[CommandDescriptor] = []
        self.max_queue_level = 0

    def _advance(self):
        self.now += self.step
        self.model.advance_to(self.now)

    def read_attr(self, name: str) -> int:
        if name == "current_time_ticks":
            self._advance()
            return self.now
        if name == "time_tick_hz":
            return TICK_HZ
        if name == "queue_depth":
            return 16
        if name == "queue_level":
            return self.model.queue_level
        if name == "capability_bitmap":
            return int(self.model.capability_bitmap)
        if name == "late_count":
            return self.model.late_count
        if name == "overflow_count":
            return self.model.overflow_count
        if name == "completion_sequence_id":
            self._advance()
            c = self.model.completion
            return c.sequence_id if (c and self.model.completion_pending) else 0
        if name == "completion_status":
            return int(Status[self.model.completion.status_name])
        if name == "completion_executed_timestamp_ticks":
            return self.model.completion.executed_timestamp_ticks or 0
        raise KeyError(name)

    def write_attr(self, name: str, value: str) -> None:
        if name == "command_descriptor":
            words = [int(w, 16) for w in value.split(",")]
            payload = b"".join(w.to_bytes(4, "little") for w in words)
            d = CommandDescriptor.unpack(payload)
            self.pushed.append(d)
            self.model.push_gpio(
                d.sequence_id,
                d.timestamp_ticks,
                d.args[0],
                d.args[1],
                late_policy="drop" if d.flags & Flag.LATE_DROP else "execute_now",
                report_completion=bool(d.flags & Flag.REPORT_COMPLETION),
            )
            self.max_queue_level = max(self.max_queue_level, self.model.queue_level)
        elif name == "completion_ack":
            self.model.ack_completion()
        else:
            raise KeyError(name)


class FakeStager:
    def __init__(self, pipeline: FhmPipelineModel):
        self.p = pipeline
        self.staged: list[int] = []
        self.pll = pipeline.init_hz

    def enable(self):
        pass

    def disable(self):
        pass

    def stage(self, hz):
        self.staged.append(hz)
        self.p.stage(hz)

    def readback_hz(self):
        return self.pll

    def park_low(self):
        pass


def plan(n=6):
    # cycle four in-band frequencies so long plans stay within the 8-slot table
    freqs = tuple(2_410 * MHZ + (i % 4) * 10 * MHZ for i in range(n))
    return HopPlan(
        frequencies_hz=freqs,
        period_ticks=2_000_000,
        settle_ticks=20_000,
        capture_ticks=100_000,
        pulse_ticks=100_000,
        lead_ticks=500_000,
    )


def test_session_runs_every_edge_keeps_queue_bounded_and_stages_one_hop_ahead():
    dev = FakeTickDevice()
    p = plan(6)
    pipeline = FhmPipelineModel(init_hz=p.init_frequency_hz)
    stager = FakeStager(pipeline)

    def on_edge(executed, expected):  # the fake PLL follows the model exactly
        stager.pll = expected

    s = TickSession(dev.read_attr, dev.write_attr, lookahead_hops=3, sleep=lambda _s: None)
    compiled = compile_plan(p, t0_ticks=dev.now, tick_hz=TICK_HZ)
    results = s.run(compiled, stager, pipeline, after_edge=on_edge)
    assert len(results) == len(compiled.edges) == 7
    assert all(r.status == "EXECUTED" and r.executed_ticks == r.requested_ticks for r in results)
    assert [r.readback_hz for r in results] == [e.commits_hz for e in compiled.edges]
    assert stager.staged == list(p.frequencies_hz), "frequencies[k] staged before edge k, once each"
    assert len(dev.pushed) == 2 * len(compiled.edges)
    # 2 descriptors per hop in flight plus the completed hop's not-yet-due falling edge.
    assert dev.max_queue_level == 2 * 3 + 1


def test_lookahead_is_clamped_so_a_16_deep_queue_never_overflows():
    dev = FakeTickDevice()
    p = plan(12)
    pipeline = FhmPipelineModel(init_hz=p.init_frequency_hz)
    stager = FakeStager(pipeline)
    s = TickSession(dev.read_attr, dev.write_attr, lookahead_hops=99, sleep=lambda _s: None)
    compiled = compile_plan(p, t0_ticks=dev.now, tick_hz=TICK_HZ)
    results = s.run(
        compiled, stager, pipeline, after_edge=lambda ex, hz: setattr(stager, "pll", hz)
    )
    assert all(r.status == "EXECUTED" for r in results)
    assert dev.max_queue_level <= 16 and dev.model.overflow_count == 0
    assert dev.model.queue_level == 0 and dev.model.gpio_out & 1 == 0, (
        "run ends with trigger low, queue empty"
    )
    assert dev.model.late_count == 0 and dev.model.overflow_count == 0
    assert dev.model.queue_level == 0
    assert not any(r.stage_late for r in results)


def test_session_reports_late_dropped_edge_without_advancing_pipeline():
    dev = FakeTickDevice(step=50_000)
    p = plan(3)
    pipeline = FhmPipelineModel(init_hz=p.init_frequency_hz)
    stager = FakeStager(pipeline)
    s = TickSession(dev.read_attr, dev.write_attr, lookahead_hops=2, sleep=lambda _s: None)
    # The clock is already past edge 0 when the plan is submitted -> LATE_DROPPED.
    dev.now = p.lead_ticks + 10
    dev.model.advance_to(dev.now)
    compiled = compile_plan(p, t0_ticks=0, tick_hz=TICK_HZ)
    results = s.run(
        compiled, stager, pipeline, after_edge=lambda ex, hz: setattr(stager, "pll", hz)
    )
    assert results[0].status == "LATE_DROPPED" and results[0].executed_ticks is None
    assert results[1].status == "EXECUTED"
    # dropped priming edge: edge 1 now commits init (pipeline did not move), and the
    # session reports what the PLL *should* read, not the plan's wish.
    assert results[1].readback_hz == p.init_frequency_hz


def test_session_requires_gpio_capability_and_tick_hz():
    dev = FakeTickDevice()
    s = TickSession(dev.read_attr, dev.write_attr, sleep=lambda _s: None)
    info = s.probe()
    assert info["time_tick_hz"] == TICK_HZ and info["queue_depth"] == 16 and info["gpio_capable"]
