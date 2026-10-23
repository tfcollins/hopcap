# SPDX-License-Identifier: MIT
"""Orchestrate one live run: probe, program, receive, hop, drain, associate, export.

Everything the run touches arrives through :class:`Components` so the whole flow is
testable with fakes; :func:`build_components` wires the real transports from a
:class:`RunConfig`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hopcap import framer as framer_mod
from hopcap.associate import AssociationResult, associate
from hopcap.export import write_bundle
from hopcap.hopctrl import HopController, HopCtrlRegs
from hopcap.plan import CompiledPlan, FhmPipelineModel, HopPlan, SlotTable, compile_plan
from hopcap.scheduler import TickSession
from hopcap.spool import iter_records


@dataclass
class RunConfig:
    plan: HopPlan
    out_dir: Path
    sample_rate_hz: int = 245_760_000
    framer: framer_mod.FramerConfig = field(default_factory=framer_mod.FramerConfig)
    provenance: dict = field(default_factory=dict)
    expected_peer: tuple[str, int] | None = ("192.0.2.1", 4991)
    lookahead_hops: int = 4
    stage_guard_ticks: int = 0
    freq_tolerance_hz: int = 0
    hopctrl_drain_timeout_ticks: int = 0
    sequence_mode: bool = False


@dataclass
class Components:
    tick_read: Callable[[str], int]
    tick_write: Callable[[str, str], None]
    mmio: Any
    framer: Any
    stager: Any
    receiver: Any
    pipeline: FhmPipelineModel
    regs: HopCtrlRegs = field(default_factory=HopCtrlRegs.from_json)
    on_compiled: Callable[[CompiledPlan], None] | None = None
    after_edge: Callable[[bool, int], None] | None = None
    host_counters: Callable[[], dict] | None = None
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = print


@dataclass
class RunReport:
    compiled: CompiledPlan
    result: AssociationResult
    manifest: dict
    out_dir: Path

    @property
    def disposition(self) -> str:
        return self.result.disposition


def run_live(cfg: RunConfig, c: Components) -> RunReport:
    log = c.log
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    spool_path = out_dir / "datagrams.spool"

    # 1. identity and baselines (fail closed before touching the radio)
    hc = HopController(c.mmio, c.regs)
    hc_id, hc_version = hc.identify()
    tick = TickSession(c.tick_read, c.tick_write, lookahead_hops=cfg.lookahead_hops, sleep=c.sleep)
    tick_info = tick.probe()
    tick.drain_completions()
    framer_before = framer_mod.counters(c.framer)
    host_before = c.host_counters() if c.host_counters else {}
    hc_before = hc.status()
    log(
        f"hop controller {hc_id} v0x{hc_version:08x}; tick {tick_info['time_tick_hz']} Hz depth {tick_info['queue_depth']}"
    )

    # 2. program the fabric, open the receiver, arm the radio
    slots = SlotTable.from_plan(cfg.plan)
    hc.program(
        cfg.plan,
        slots,
        sequence_mode=cfg.sequence_mode,
        drain_timeout_ticks=cfg.hopctrl_drain_timeout_ticks,
    )
    framer_mod.configure(c.framer, cfg.framer)
    c.receiver.start()
    tick.park_trigger_low(lead_ticks=max(1, tick_info["time_tick_hz"] // 100))
    c.stager.enable()
    hc.require_idle()

    # 3. compile against the live clock only now, so lead_ticks is not eaten by setup
    t0 = tick.now()
    compiled = compile_plan(cfg.plan, t0_ticks=t0, tick_hz=tick_info["time_tick_hz"])
    if c.on_compiled:
        c.on_compiled(compiled)
    log(f"plan {cfg.plan.name}: {len(compiled.edges)} edges from t0={t0}, {len(slots)} slots")

    # 4. hop
    try:
        edges = tick.run(
            compiled,
            c.stager,
            c.pipeline,
            after_edge=c.after_edge,
            stage_guard_ticks=cfg.stage_guard_ticks,
        )
    finally:
        c.stager.disable()

    # 5. drain and collect
    executed = sum(1 for e in edges if e.executed)
    hc_after = hc.wait_drained(expected_events=hc_before.epoch + executed, sleep=c.sleep)
    hc.disable()
    rx_summary = c.receiver.stop()
    framer_mod.disable(c.framer)
    framer_after = framer_mod.counters(c.framer)
    host_after = c.host_counters() if c.host_counters else {}
    tick_after = {
        "late_count": c.tick_read("late_count"),
        "overflow_count": c.tick_read("overflow_count"),
    }

    # 6. associate and export
    with spool_path.open("rb") as fp:
        records = list(iter_records(fp, strict=False))
    result = associate(
        records,
        compiled,
        edges,
        sample_rate_hz=cfg.sample_rate_hz,
        samples_per_packet=cfg.framer.samples_per_packet,
        epoch_base=hc_before.epoch,
        expected_peer=cfg.expected_peer,
        freq_tolerance_hz=cfg.freq_tolerance_hz,
    )
    counters = {
        "framer_before": framer_before,
        "framer_after": framer_after,
        "framer": framer_mod.delta(framer_before, framer_after),
        "host_before": host_before,
        "host_after": host_after,
        "receiver": rx_summary,
    }
    extra = {
        "tick": {
            **tick_info,
            "t0_ticks": t0,
            "late_count_delta": tick_after["late_count"] - tick_info["late_count"],
            "overflow_count_delta": tick_after["overflow_count"] - tick_info["overflow_count"],
        },
        "hopctrl": {
            "id": hc_id,
            "version": hc_version,
            "before": hc_before.as_dict(),
            "after": hc_after.as_dict(),
        },
        "edges": [vars(e) for e in edges],
    }
    write_bundle(
        out_dir,
        compiled=compiled,
        result=result,
        records=records,
        sample_rate_hz=cfg.sample_rate_hz,
        provenance=cfg.provenance,
        counters=counters,
    )
    import json

    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(extra)
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    log(
        f"run {result.disposition}: {sum(h.complete for h in result.hops)}/{len(result.hops)} hops complete"
    )
    return RunReport(compiled=compiled, result=result, manifest=manifest, out_dir=out_dir)
