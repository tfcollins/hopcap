# SPDX-License-Identifier: MIT
"""Drive Tick through a compiled plan: bounded lookahead submission, one-entry completion
window discipline, Talise staging one hop ahead, PLL readback after every edge."""

from __future__ import annotations

import time
from collections.abc import Callable

from tick import Capability, CommandDescriptor, CommandID, Flag, Status, TargetFamily
from tick.ffh import descriptor_csv, wait_for_completion

from hopcap.associate import EdgeResult
from hopcap.descriptors import RISE_SEQ_BASE, descriptors_for_edge, gpio_value
from hopcap.plan import CompiledPlan, FhmPipelineModel

ReadAttr = Callable[[str], int]
WriteAttr = Callable[[str, str], None]


class SchedulerError(RuntimeError):
    pass


class TickSession:
    def __init__(
        self,
        read_attr: ReadAttr,
        write_attr: WriteAttr,
        *,
        lookahead_hops: int = 4,
        poll_s: float = 0.002,
        sleep: Callable[[float], None] = time.sleep,
        completion_timeout_s: float = 5.0,
    ):
        self.read = read_attr
        self.write = write_attr
        self.lookahead = lookahead_hops
        self.poll_s = poll_s
        self.sleep = sleep
        self.completion_timeout_s = completion_timeout_s

    # -- probing ------------------------------------------------------------
    def probe(self) -> dict:
        caps = Capability(self.read("capability_bitmap"))
        info = {
            "time_tick_hz": self.read("time_tick_hz"),
            "queue_depth": self.read("queue_depth"),
            "capability_bitmap": int(caps),
            "gpio_capable": bool(caps & Capability.GPIO_TARGET),
            "late_count": self.read("late_count"),
            "overflow_count": self.read("overflow_count"),
        }
        if not info["gpio_capable"]:
            raise SchedulerError("Tick build has no GPIO target capability")
        return info

    def drain_completions(self, limit: int = 64) -> int:
        n = 0
        while self.read("completion_sequence_id") != 0 and n < limit:
            self.write("completion_ack", "1")
            n += 1
        return n

    def now(self) -> int:
        return self.read("current_time_ticks")

    def submit(self, d: CommandDescriptor) -> None:
        self.write("command_descriptor", descriptor_csv(d))

    def park_trigger_low(self, *, lead_ticks: int, seq: int = 0x0F00) -> None:
        """Drive gpio bit 0 low once before FHM is enabled (demo step)."""
        d = CommandDescriptor(
            command_id=CommandID.GPIO_WRITE,
            target_id=TargetFamily.GPIO,
            timestamp_ticks=self.now() + lead_ticks,
            flags=Flag.REPORT_COMPLETION,
            sequence_id=seq,
            args=(0xF, gpio_value(0, False), 0, 0),
        )
        self.submit(d)
        wait_for_completion(
            self.read,
            self.write,
            seq,
            timeout_s=self.completion_timeout_s,
            poll_s=self.poll_s,
            sleep=self.sleep,
        )

    # -- the run --------------------------------------------------------------
    def run(
        self,
        compiled: CompiledPlan,
        stager,
        pipeline: FhmPipelineModel,
        *,
        after_edge: Callable[[bool, int], None] | None = None,
        stage_guard_ticks: int = 0,
    ) -> list[EdgeResult]:
        edges = compiled.edges
        # Peak occupancy is 2*lookahead + 1: the just-completed hop's falling edge is still
        # queued when the next hop's pair is submitted.  Clamp so a 16-deep queue holds it.
        lookahead = max(1, min(self.lookahead, (self.read("queue_depth") - 1) // 2))
        results: list[EdgeResult] = []
        submitted = 0

        def submit_up_to(k: int) -> None:
            nonlocal submitted
            while submitted < len(edges) and submitted <= k:
                for d in descriptors_for_edge(edges[submitted]):
                    self.submit(d)
                submitted += 1

        # Stage frequencies[0] before edge 0 fires, then keep one hop ahead.
        if edges[0].stage_before_hz is not None:
            stager.stage(edges[0].stage_before_hz)
        submit_up_to(lookahead - 1)
        for k, e in enumerate(edges):
            seq = RISE_SEQ_BASE + e.index
            status, executed = wait_for_completion(
                self.read,
                self.write,
                seq,
                timeout_s=self.completion_timeout_s,
                poll_s=self.poll_s,
                sleep=self.sleep,
            )
            executed_ok = status in (Status.EXECUTED, Status.LATE_EXECUTED)
            expected = pipeline.on_edge(executed=executed_ok)
            host_mono = time.monotonic_ns()
            # Stage what the *next* edge needs before it fires.
            stage_late = False
            if k + 1 < len(edges) and edges[k + 1].stage_before_hz is not None:
                stager.stage(edges[k + 1].stage_before_hz)
                if self.now() > edges[k + 1].rise_ticks - stage_guard_ticks:
                    stage_late = True
            if after_edge is not None:
                after_edge(executed_ok, expected)
            readback = stager.readback_hz()
            results.append(
                EdgeResult(
                    index=e.index,
                    slot=e.slot,
                    commits_hz=expected if executed_ok else e.commits_hz,
                    requested_ticks=e.rise_ticks,
                    executed_ticks=executed if executed_ok else None,
                    status=status.name,
                    readback_hz=readback,
                    stage_late=stage_late,
                    host_mono_ns=host_mono,
                )
            )
            submit_up_to(k + lookahead)
        # Leave with the trigger low and the queue empty: wait for the final falling edge.
        deadline = time.monotonic() + self.completion_timeout_s
        while self.now() <= edges[-1].fall_ticks:
            if time.monotonic() >= deadline:
                raise SchedulerError("final falling edge never came due")
            self.sleep(self.poll_s)
        return results
