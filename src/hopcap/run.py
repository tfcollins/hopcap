# SPDX-License-Identifier: MIT
"""Orchestrate one live run: probe, program, receive, hop, drain, associate, export.

Everything the run touches arrives through :class:`Components` so the whole flow is
testable with fakes; :func:`build_components_from_board` (ZU11EG) and
:func:`build_components_adrv9361` wire the real transports.

``RunConfig.platform`` selects the hop model. ZU11EG: a :class:`~hopcap.plan.HopPlan`, the Talise
stager one hop behind, the pipeline model. ADRV9361: a :class:`~hopcap.plan.FastLockPlan`; the
radio adapter is the :class:`~hopcap.fastlock.FastLockBank` (``prepare`` stores and recalls
profile 0, Tick parks CTRL_IN at 0, ``enable`` sets pin select), nothing is staged, each edge
commits its own slot, Tick's epoch is aligned to the framer's PPS second before t0 is read, and
the measured PPS period is recorded before and after the hops.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as _replace
from pathlib import Path
from typing import Any

from hopcap import framer as framer_mod
from hopcap import stamps as stamps_mod
from hopcap.associate import AssociationResult, associate
from hopcap.export import write_bundle
from hopcap.hopctrl import HopController, HopCtrlRegs
from hopcap.plan import (
    CompiledPlan,
    FastLockPlan,
    FhmPipelineModel,
    HopPlan,
    SlotTable,
    compile_fastlock_plan,
    compile_plan,
)
from hopcap.platform import ZU11EG, Platform, ticks_to_us
from hopcap.scheduler import TickSession
from hopcap.spool import iter_records

FRAMER_IIO_NAME = "vrt49_framer"  # iio-vrt49-buffer.c indio_dev->name (probe on tron 2026-10-23)
TICK_IIO_NAME = "axi-timed-command-scheduler"


@dataclass
class RunConfig:
    plan: HopPlan | FastLockPlan
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
    platform: Platform = ZU11EG  # FastLock platforms take the plan's sample rate
    # Gapless timing run (FastLock): the hop controller is programmed and then disabled, which
    # forces its gate open, so samples stream continuously across every edge and the hop is
    # visible in the capture. No Context packets are stamped; hop landing is measured from the
    # loopback tone instead (hopcap.hoptiming).
    gapless: bool = False
    keep_spool: bool = True  # gapless: False deletes the raw spool once it is analysed


@dataclass
class Components:
    tick_read: Callable[[str], int]
    tick_write: Callable[[str, str], None]
    mmio: Any
    framer: Any
    stager: Any
    receiver: Any
    pipeline: FhmPipelineModel | None  # None: each edge commits its own slot (FastLock)
    regs: HopCtrlRegs = field(default_factory=HopCtrlRegs.from_json)
    on_compiled: Callable[[CompiledPlan], None] | None = None
    after_edge: Callable[[bool, int], None] | None = None
    host_counters: Callable[[], dict] | None = None
    sleep: Callable[[float], None] = time.sleep
    # The clock `sleep` advances; the time-base arm check measures elapsed time with it.
    monotonic: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = print
    # After the framer time base is LOCKED, before the stream starts (ADRV9361: Tick epoch).
    time_align: Callable[[], dict] | None = None
    # Measured beats per PPS (framer TB_PPS_PERIOD), recorded before and after the hops.
    pps_period: Callable[[], dict] | None = None


def build_components_from_board(
    shell,
    *,
    board_ip: str,
    out_dir: Path,
    bind: str = "192.0.2.2:4991",
    rx_interface: str = "enp4s0f0np0",
    rx_cpu: int | None = None,
    hopctrl_base: int = 0x84AC0000,
    iio_uri: str | None = None,
    receiver: str = "c",
) -> Components:
    """Wire the real transports: iio_attr for Tick/Talise, the on-target helper for MMIO and
    framer sysfs over `shell`, and the C spooler on the host NIC."""
    from hopcap.plan import FhmPipelineModel as _Pipeline
    from hopcap.receiver import CReceiver, PyReceiver, host_rx_counters
    from hopcap.talise import FhmStager
    from hopcap.transport import (
        HelperMmio,
        HelperSysfs,
        find_fhm_channel_cli,
        find_iio_device_dir,
        iio_channel_cli_io,
        iio_device_cli_io,
    )

    uri = iio_uri or f"ip:{board_ip}"
    mmio = HelperMmio(shell, base=hopctrl_base)
    mmio.ensure_uploaded()
    framer_dir = find_iio_device_dir(shell, FRAMER_IIO_NAME)
    framer = HelperSysfs(shell, device_dir=framer_dir)
    tick_read, tick_write = iio_device_cli_io(uri, TICK_IIO_NAME)
    lo_chan = find_fhm_channel_cli(uri, "adrv9009-phy")
    lo_read, lo_write = iio_channel_cli_io(uri, "adrv9009-phy", lo_chan, output=True)
    stager = FhmStager(lo_read, lo_write)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    host, port = bind.rsplit(":", 1)
    spool = out_dir / "datagrams.spool"
    rx = (
        CReceiver(bind=(host, int(port)), spool_path=spool, cpu=rx_cpu)
        if receiver == "c"
        else PyReceiver(bind=(host, int(port)), spool_path=spool)
    )
    init_hz = int(str(lo_read("frequency_hopping_mode")).split()[-1] or 2_450_000_000)
    comps = Components(
        tick_read=tick_read,
        tick_write=tick_write,
        mmio=mmio,
        framer=framer,
        stager=stager,
        receiver=rx,
        pipeline=_Pipeline(init_hz=2_450_000_000),
        host_counters=lambda: host_rx_counters(rx_interface),
    )
    comps.out_dir = out_dir  # type: ignore[attr-defined]
    comps.lo_channel = lo_chan  # type: ignore[attr-defined]
    comps.pll_at_connect_hz = init_hz  # type: ignore[attr-defined]
    return comps


@dataclass
class RunReport:
    compiled: CompiledPlan
    result: AssociationResult | None
    manifest: dict
    out_dir: Path

    @property
    def disposition(self) -> str:
        if self.result is None:  # gapless timing run: no association
            return self.manifest.get("disposition", "incomplete")
        return self.result.disposition


def build_components_adrv9361(
    shell,
    *,
    iio_uri: str,
    out_dir: Path,
    plat: Platform | None = None,
    bind: str | None = None,
    rx_cpu: int | None = None,
    receiver: str = "c",
) -> Components:
    """ADRV9361-Z7035 transports: Tick over network IIO, the FastLock bank (channel/debug
    attributes over IIO, setup registers over the board shell), HOP2 and the framer's MMIO-only
    time-base registers through the on-target helper, nemo enp7s0 as the receiver."""
    from hopcap import timebase
    from hopcap.fastlock import FastLockBank, PhyRegs
    from hopcap.platform import ADRV9361
    from hopcap.receiver import CReceiver, PyReceiver, host_rx_counters
    from hopcap.transport import (
        HelperMmio,
        HelperSysfs,
        find_iio_device_dir,
        iio_channel_cli_io,
        iio_debug_cli_write,
        iio_device_cli_io,
    )

    plat = plat or ADRV9361
    mmio = HelperMmio(shell, base=plat.hopctrl_base)
    mmio.ensure_uploaded()
    framer_mmio = HelperMmio(shell, base=plat.framer_base)
    framer = HelperSysfs(shell, device_dir=find_iio_device_dir(shell, plat.framer_iio_name))
    tick_read, tick_write = iio_device_cli_io(iio_uri, plat.tick_iio_name)
    writers = {
        ch: iio_channel_cli_io(iio_uri, plat.phy_iio_name, ch, output=True)[1]
        for ch in ("altvoltage0", "altvoltage1")
    }
    bank = FastLockBank(
        write_channel=lambda ch, attr, value: writers[ch](attr, value),
        write_debug=iio_debug_cli_write(iio_uri, plat.phy_iio_name),
        regs=PhyRegs(shell, device=plat.phy_iio_name),
    )
    rx_nic = plat.receiver()
    host, port = (bind or f"{rx_nic.ip}:{plat.dest_port}").rsplit(":", 1)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    spool = out_dir / "datagrams.spool"
    rx = (
        CReceiver(bind=(host, int(port)), spool_path=spool, cpu=rx_cpu)
        if receiver == "c"
        else PyReceiver(bind=(host, int(port)), spool_path=spool)
    )

    def pps_period() -> dict:
        return timebase.read_pps_period(framer_mmio)

    def time_align() -> dict:
        period = timebase.check_pps_period(**pps_period())
        aligned = timebase.TickAligner(
            shell, plat.tick_hz, tick_name=plat.tick_iio_name, framer_name=plat.framer_iio_name
        ).align()
        return {"pps_period": period, "tick_alignment": aligned}

    comps = Components(
        tick_read=tick_read,
        tick_write=tick_write,
        mmio=mmio,
        framer=framer,
        stager=bank,
        receiver=rx,
        pipeline=None,
        host_counters=lambda: host_rx_counters(rx_nic.interface),
        time_align=time_align,
        pps_period=pps_period,
    )
    comps.out_dir = out_dir  # type: ignore[attr-defined]
    comps.framer_mmio = framer_mmio  # type: ignore[attr-defined]
    return comps


def run_live(cfg: RunConfig, c: Components) -> RunReport:
    log = c.log
    plat = cfg.platform
    fastlock = isinstance(cfg.plan, FastLockPlan)
    if fastlock != (plat.hop_mode == "fastlock"):
        raise ValueError(
            f"platform {plat.name} hops by {plat.hop_mode}; plan is {type(cfg.plan).__name__}"
        )
    sample_rate_hz = cfg.plan.sample_rate_hz if fastlock else cfg.sample_rate_hz
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    spool_path = out_dir / "datagrams.spool"

    # 1. identity and baselines (fail closed before touching the radio)
    hc = HopController(c.mmio, c.regs)
    hc_id, hc_version = hc.identify()
    tick = TickSession(
        c.tick_read,
        c.tick_write,
        lookahead_hops=cfg.lookahead_hops,
        sleep=c.sleep,
        bits=plat.bits,
        report_completion=not fastlock,
    )
    tick_info = tick.probe()
    tick_hz = tick_info["time_tick_hz"]
    if plat.tick_hz_strict:
        plat.check_tick_hz(tick_hz)
    tick.drain_completions()
    framer_before = framer_mod.counters(c.framer)
    host_before = c.host_counters() if c.host_counters else {}
    hc_before = hc.status()
    log(
        f"hop controller {hc_id} v0x{hc_version:08x}; tick {tick_info['time_tick_hz']} Hz depth {tick_info['queue_depth']}"
    )

    # Transport latency: one Tick attribute round trip in ticks.  Every submit/stage/poll costs
    # about one of these, so the lead must cover setup and the period must cover staging.
    n0 = tick.now()
    n1 = tick.now()
    rtt = max(1, n1 - n0)
    lookahead = max(1, min(cfg.lookahead_hops, (tick_info["queue_depth"] - 1) // 2))
    setup_calls = 2 * lookahead + 6
    staging_floor = 6 * rtt  # stage write + readback + completion polls, with margin
    if fastlock:
        fl_plan: FastLockPlan = cfg.plan
        lead = max(fl_plan.to_hop_plan(tick_hz).lead_ticks, setup_calls * rtt * 2)
        if lead != fl_plan.to_hop_plan(tick_hz).lead_ticks:
            fl_plan = _replace(fl_plan, lead_us=ticks_to_us(lead, tick_hz))
        plan = fl_plan.to_hop_plan(tick_hz)
        slots = fl_plan.slot_table()  # by index: TX-only slots share an RX LO
    else:
        lead = max(cfg.plan.lead_ticks, setup_calls * rtt * 2)
        plan = cfg.plan if lead == cfg.plan.lead_ticks else _replace(cfg.plan, lead_ticks=lead)
        slots = SlotTable.from_plan(plan)
    log(f"transport rtt ~{rtt} ticks; lead {lead} ticks; staging floor {staging_floor} ticks")

    # 2. program the fabric, open the receiver, arm the radio (gate stays shut until edge 0)
    receiver_started = False
    stager_enabled = False
    edges = None
    tb_armed: dict = {}
    try:
        hc.program(
            plan,
            slots,
            tick_hz=tick_hz,
            sequence_mode=cfg.sequence_mode,
            drain_timeout_ticks=cfg.hopctrl_drain_timeout_ticks,
        )
        if cfg.gapless:
            if not fastlock:
                raise ValueError("gapless timing runs need a FastLock plan")
            hc.disable()  # gate forced open for the whole run
        tb_armed = framer_mod.configure(
            c.framer, cfg.framer, sleep=c.sleep, monotonic=c.monotonic, after_arm=c.time_align
        )
        pps_before = c.pps_period() if c.pps_period else None
        c.receiver.start()
        receiver_started = True
        if fastlock:
            c.stager.prepare(fl_plan)  # store RX+TX profiles, recall 0 (pin select still off)
        tick.park_trigger_low(lead_ticks=max(tick_hz // 100, 4 * rtt))
        c.stager.enable()  # ZU11EG: FHM GPIO mode; ADRV9361: profile pin select on
        stager_enabled = True
        if not cfg.gapless:  # gapless: the controller is disabled on purpose (gate open)
            hc.require_idle()

        # 3. compile against the live clock only now, so the lead is not eaten by setup
        t0 = tick.now()
        if fastlock:
            compiled = compile_fastlock_plan(
                fl_plan, t0_ticks=t0, tick_hz=tick_hz, min_period_ticks=staging_floor
            )
        else:
            compiled = compile_plan(
                plan, t0_ticks=t0, tick_hz=tick_hz, min_period_ticks=staging_floor
            )
        if c.on_compiled:
            c.on_compiled(compiled)
        log(f"plan {plan.name}: {len(compiled.edges)} edges from t0={t0}, {len(slots)} slots")

        # 4. hop
        edges = tick.run(
            compiled,
            c.stager,
            c.pipeline,
            after_edge=c.after_edge,
            stage_guard_ticks=cfg.stage_guard_ticks,
        )
    finally:
        # Teardown order matters: stop the stream (framer) BEFORE stopping the receiver, which
        # waits for the socket to go idle; the hop controller is released last.
        if stager_enabled:
            try:
                c.stager.disable()
            except Exception as exc:
                log(f"warning: radio hop-input disable failed: {exc}")
        if edges is None:
            # failure path: leave the board quiet and the port free
            for step in (
                lambda: framer_mod.disable(c.framer),
                (lambda: c.receiver.stop()) if receiver_started else (lambda: None),
                hc.disable,
            ):
                try:
                    step()
                except Exception as exc:
                    log(f"warning: cleanup step failed: {exc}")

    # 5. drain and collect
    executed = sum(1 for e in edges if e.executed)
    if cfg.gapless:
        c.sleep(0.05)  # let the last post-edge window arrive
        hc_after = hc.status()
    else:
        hc_after = hc.wait_drained(expected_events=hc_before.epoch + executed, sleep=c.sleep)
    framer_mod.disable(c.framer)
    framer_after = framer_mod.counters(c.framer)
    tb_after = framer_mod.time_base_state(c.framer) if cfg.framer.time_base else None
    pps_after = c.pps_period() if c.pps_period else None
    rx_summary = c.receiver.stop()
    hc.disable()
    host_after = c.host_counters() if c.host_counters else {}
    tick_after = {
        "late_count": c.tick_read("late_count"),
        "overflow_count": c.tick_read("overflow_count"),
    }

    # 6. associate and export
    with spool_path.open("rb") as fp:
        records = list(iter_records(fp, strict=False))
    if cfg.gapless:
        return _finish_gapless(
            cfg,
            c,
            compiled=compiled,
            edges=edges,
            records=records,
            out_dir=out_dir,
            tick_info=tick_info,
            tb_armed=tb_armed,
            pps_before=pps_before,
            pps_after=pps_after,
            tb_after=tb_after,
            framer_before=framer_before,
            framer_after=framer_after,
            rx_summary=rx_summary,
            hc_before=hc_before,
            hc_after=hc_after,
            tick_after=tick_after,
            plan=fl_plan,
            t0=t0,
            rtt=rtt,
            lead=lead,
        )
    result = associate(
        records,
        compiled,
        edges,
        sample_rate_hz=sample_rate_hz,
        samples_per_packet=cfg.framer.samples_per_packet,
        epoch_base=hc_before.epoch,
        expected_peer=cfg.expected_peer,
        freq_tolerance_hz=cfg.freq_tolerance_hz,
        # loss before the run shows as one packet-count skip at the first data packet; only
        # forgiven when the tap/framer loss counters did not move during the run
        pre_run_loss_mark=framer_mod.delta(framer_before, framer_after).get(
            "v49_tx_dropped_packets", 1
        )
        == 0,
    )
    q32 = (
        tb_armed["after_arm"]["ps_per_sample_q32"]
        if tb_armed
        else stamps_mod.q32_for_rate(sample_rate_hz)
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
        "platform": plat.name,
        "tick": {
            **tick_info,
            "t0_ticks": t0,
            "transport_rtt_ticks": rtt,
            "lead_ticks": lead,
            "staging_floor_ticks": staging_floor,
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
        "time_base": {
            "armed": tb_armed,
            "after": tb_after,
            "stamps": stamps_mod.check(
                records, ps_per_sample_q32=q32, expected_peer=cfg.expected_peer
            ),
            "hop_spacing_residual_ps": stamps_mod.hop_spacing_residuals(
                result.hops, tick_hz=compiled.tick_hz
            ),
            "pps_period_before": pps_before,
            "pps_period_after": pps_after,
        },
    }
    if fastlock:
        extra["fastlock"] = {
            "plan": compiled.fastlock.as_dict(),
            "bank": getattr(c.stager, "record", None),
            "controller_hz": plat.controller_hz,
        }
    write_bundle(
        out_dir,
        compiled=compiled,
        result=result,
        records=records,
        sample_rate_hz=sample_rate_hz,
        provenance=cfg.provenance,
        counters=counters,
    )
    import json

    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(extra)
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    from hopcap.export import write_sums

    write_sums(out_dir)  # the manifest changed after write_bundle hashed it
    log(
        f"run {result.disposition}: {sum(h.complete for h in result.hops)}/{len(result.hops)} hops complete"
    )
    return RunReport(compiled=compiled, result=result, manifest=manifest, out_dir=out_dir)


def _finish_gapless(
    cfg,
    c,
    *,
    compiled,
    edges,
    records,
    out_dir,
    tick_info,
    tb_armed,
    pps_before,
    pps_after,
    tb_after,
    framer_before,
    framer_after,
    rx_summary,
    hc_before,
    hc_after,
    tick_after,
    plan,
    t0,
    rtt,
    lead,
) -> RunReport:
    """Gapless FastLock run: measure where every executed hop lands in the capture."""
    import json

    from hopcap import hoptiming
    from hopcap.export import write_sums

    tick_hz = tick_info["time_tick_hz"]
    q32 = tb_armed["after_arm"]["ps_per_sample_q32"]
    pps = pps_after or pps_before or {}
    beats_per_pps = int(pps.get("beats_per_pps") or plan.sample_rate_hz)
    stream = hoptiming.SampleStream.from_records(
        records,
        tick_hz=tick_hz,
        ps_per_sample_q32=q32,
        beats_per_pps=beats_per_pps,
        expected_peer=cfg.expected_peer,
    )
    slots = [(s.rx_lo_hz, s.tx_lo_hz) for s in plan.slots]
    f_dds = getattr(c.stager, "f_dds_hz", None) or plan.f_dds_hz
    hops, prev = [], 0  # profile 0 is live before hop 0
    for e in edges:
        if e.executed:
            hops.append(
                hoptiming.measure_hop(
                    stream,
                    index=e.index,
                    executed_ticks=e.executed_ticks,
                    from_slot=prev,
                    to_slot=e.slot,
                    slots=slots,
                    f_dds_hz=f_dds,
                    tick_hz=tick_hz,
                )
            )
        prev = e.slot
    summary = hoptiming.summarize(hops)
    # No hop without an edge: half-way to the next edge the tone is still this slot's.
    steady = []
    for k, e in enumerate(edges):
        if not e.executed:
            continue
        nxt = edges[k + 1].requested_ticks if k + 1 < len(edges) else None
        mid = (e.requested_ticks + nxt) // 2 if nxt else e.requested_ticks + int(0.1 * tick_hz)
        st = hoptiming.steady_tone(
            stream, at_ticks=mid, slots=slots, f_dds_hz=f_dds, tick_hz=tick_hz
        )
        st.update({"index": e.index, "expected_slot": e.slot, "ok": st["slot"] == e.slot})
        steady.append(st)
    late = tick_after["late_count"] - tick_info["late_count"]
    over = tick_after["overflow_count"] - tick_info["overflow_count"]
    # Completion-free edges are only trustworthy if Tick dropped none (LATE_DROP) and the
    # queue never overflowed; the capture then shows every one landing.
    complete = (
        all(h.latency_ns is not None for h in hops)
        and len(hops) == len(edges)
        and late == 0
        and over == 0
        and all(st["ok"] for st in steady)
    )
    manifest = {
        "schema": "hopcap-gapless-1",
        "mode": "gapless",
        "plan": plan.name,
        "platform": cfg.platform.name,
        "provenance": cfg.provenance,
        "disposition": "ok" if complete else "incomplete",
        "f_dds_hz": f_dds,
        "slots": slots,
        "edges": [vars(e) for e in edges],
        "hop_timing": [h.as_dict() for h in hops],
        "hop_timing_summary": summary,
        "steady_tone": steady,
        "samples": len(stream.iq),
        "tick": {
            **tick_info,
            "t0_ticks": t0,
            "transport_rtt_ticks": rtt,
            "lead_ticks": lead,
            "late_count_delta": tick_after["late_count"] - tick_info["late_count"],
            "overflow_count_delta": tick_after["overflow_count"] - tick_info["overflow_count"],
        },
        "hopctrl": {"before": hc_before.as_dict(), "after": hc_after.as_dict()},
        "time_base": {
            "armed": tb_armed,
            "after": tb_after,
            "pps_period_before": pps_before,
            "pps_period_after": pps_after,
            "beats_per_pps_used": beats_per_pps,
            "stamps": stamps_mod.check(
                records, ps_per_sample_q32=q32, expected_peer=cfg.expected_peer
            ),
        },
        "counters": {
            "framer": framer_mod.delta(framer_before, framer_after),
            "receiver": rx_summary,
        },
        "fastlock": {
            "plan": compiled.fastlock.as_dict(),
            "bank": getattr(c.stager, "record", None),
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    if not cfg.keep_spool:  # ~1.7 GB per gapless run; the manifest holds the measurements
        (out_dir / "datagrams.spool").unlink(missing_ok=True)
    write_sums(out_dir)
    a = summary.get("all", {})
    c.log(
        f"gapless run {manifest['disposition']}: {summary['measured']}/{len(edges)} hops measured; "
        f"latency mean {a.get('mean_ns', float('nan')):.1f} ns spread {a.get('spread_ns', float('nan')):.1f} ns"
    )
    return RunReport(compiled=compiled, result=None, manifest=manifest, out_dir=out_dir)
