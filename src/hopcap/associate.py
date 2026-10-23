# SPDX-License-Identifier: MIT
"""Join the received VRT stream to the Tick edge results, one CaptureRecord per edge.

Rules (fail closed, never guess):

* IF Data is segmented into *windows*: a window starts after a Context, and is also split
  on a timestamp gap longer than half the plan's inter-hop silence (period - capture), so
  a lost Context cannot merge two hops while a run of lost packets inside one capture
  cannot split it.
* A window is tagged by the Context that arrived immediately before it, nothing else.
  A window with no such Context is ``invalid`` (``no_context``).
* Executed edges are matched to Contexts in order of epoch (controller epoch increments
  once per event).  An edge Tick reported as not executed gets no Context and no data and
  is ``invalid`` (``edge_not_executed``).
* Completeness: received samples within one packet of the configured capture length and
  no packet-count gaps.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

from hopcap import vrt
from hopcap.plan import CompiledPlan
from hopcap.spool import Record

PS = 10**12


@dataclass(frozen=True)
class EdgeResult:
    index: int
    slot: int
    commits_hz: int
    requested_ticks: int
    executed_ticks: int | None
    status: str
    readback_hz: int | None
    stage_late: bool
    host_mono_ns: int | None = None

    @property
    def executed(self) -> bool:
        return self.status in ("EXECUTED", "LATE_EXECUTED")


@dataclass
class Window:
    context: vrt.Context | None
    context_record: Record | None
    packets: list[tuple[Record, vrt.IfData]] = field(default_factory=list)
    gaps: int = 0
    last_count: int | None = None   # pkt_count continuity, seeded from the Context

    @property
    def samples(self) -> int:
        return sum(p.payload_words for _, p in self.packets)

    @property
    def first_sample_ps(self) -> int | None:
        if not self.packets:
            return None
        p = self.packets[0][1]
        return p.int_secs * PS + p.frac_ps


@dataclass
class CaptureRecord:
    index: int
    priming: bool
    slot: int
    planned_hz: int
    readback_hz: int | None
    context_hz: int | None
    epoch: int | None
    tick_requested: int
    tick_executed: int | None
    tick_status: str
    stage_late: bool
    context_mono_ns: int | None
    first_sample_ps: int | None
    last_sample_ps: int | None
    first_arrival_mono_ns: int | None
    packets: int
    samples: int
    expected_samples: int
    gaps: int
    lost_packets_estimate: int
    complete: bool
    disposition: str
    reasons: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Counts:
    records: int = 0
    data: int = 0
    context: int = 0
    malformed: int = 0
    foreign_peer: int = 0
    other_type: int = 0
    windows: int = 0
    orphan_contexts: int = 0
    orphan_windows: int = 0
    epoch_skips: int = 0


@dataclass
class AssociationResult:
    hops: list[CaptureRecord]
    counts: Counts
    disposition: str
    epochs_seen: list[int]

    def as_dict(self) -> dict:
        return {"hops": [h.as_dict() for h in self.hops], "counts": asdict(self.counts),
                "disposition": self.disposition, "epochs_seen": self.epochs_seen}


def _segment(records: Iterable[Record], *, expected_peer: tuple[str, int] | None, gap_ps: int,
             counts: Counts) -> tuple[list[Window], list[tuple[Record, vrt.Context]]]:
    windows: list[Window] = []
    contexts: list[tuple[Record, vrt.Context]] = []
    cur: Window | None = None
    pending_ctx: tuple[Record, vrt.Context] | None = None
    last_pkt: vrt.IfData | None = None
    for rec in records:
        counts.records += 1
        if expected_peer and (rec.peer_ip, rec.peer_port) != expected_peer:
            counts.foreign_peer += 1
            continue
        try:
            pkt = vrt.parse(rec.data)
        except vrt.VrtError:
            counts.malformed += 1
            continue
        if isinstance(pkt, vrt.Context):
            counts.context += 1
            contexts.append((rec, pkt))
            if pending_ctx is not None:
                counts.orphan_contexts += 1  # a Context with no data behind it
            pending_ctx = (rec, pkt)
            cur = None
            last_pkt = None
            continue
        if not isinstance(pkt, vrt.IfData):
            counts.other_type += 1
            continue
        counts.data += 1
        ts = pkt.int_secs * PS + pkt.frac_ps
        split_on_gap = (cur is not None and last_pkt is not None
                        and ts - (last_pkt.int_secs * PS + last_pkt.frac_ps) > gap_ps)
        if cur is None or split_on_gap:
            if pending_ctx is not None:
                cur = Window(context=pending_ctx[1], context_record=pending_ctx[0],
                             last_count=pending_ctx[1].pkt_count)
                pending_ctx = None
            else:
                cur = Window(context=None, context_record=None)
                counts.orphan_windows += 1
            windows.append(cur)
            last_pkt = None
        if cur.last_count is not None and ((cur.last_count + 1) & 0xF) != pkt.pkt_count:
            cur.gaps += ((pkt.pkt_count - cur.last_count - 1) & 0xF) or 1
        cur.last_count = pkt.pkt_count
        cur.packets.append((rec, pkt))
        last_pkt = pkt
    if pending_ctx is not None:
        counts.orphan_contexts += 1
    counts.windows = len(windows)
    return windows, contexts


def default_gap_ps(compiled: CompiledPlan) -> int:
    """Half the silence between captures: period - capture, in picoseconds."""
    silence_ticks = max(1, compiled.plan.period_ticks - compiled.plan.capture_ticks)
    return silence_ticks * PS // (2 * compiled.tick_hz)


def associate(records: Iterable[Record], compiled: CompiledPlan, edges: list[EdgeResult], *,
              sample_rate_hz: int, samples_per_packet: int, epoch_base: int,
              expected_peer: tuple[str, int] | None = None, gap_ps: int | None = None,
              freq_tolerance_hz: int = 0) -> AssociationResult:
    counts = Counts()
    if gap_ps is None:
        gap_ps = default_gap_ps(compiled)
    windows, _contexts = _segment(records, expected_peer=expected_peer, gap_ps=gap_ps, counts=counts)
    expected_samples = compiled.plan.capture_samples(sample_rate_hz=sample_rate_hz, tick_hz=compiled.tick_hz)
    tagged = [w for w in windows if w.context is not None]
    by_epoch = {w.context.hop_epoch: w for w in tagged}
    epochs_seen = sorted(by_epoch)

    hops: list[CaptureRecord] = []
    next_epoch = epoch_base + 1
    prev_epoch: int | None = None
    for e, edge in zip(compiled.edges, edges):
        reasons: list[str] = []
        win: Window | None = None
        epoch: int | None = None
        if not edge.executed:
            reasons.append("edge_not_executed")
        else:
            epoch = next_epoch
            next_epoch += 1
            win = by_epoch.pop(epoch, None)
            if win is None:
                reasons.append("no_context")
                epoch = None
            else:
                if prev_epoch is not None and win.context.hop_epoch != prev_epoch + 1:
                    counts.epoch_skips += 1
                prev_epoch = win.context.hop_epoch
        if edge.stage_late:
            reasons.append("stage_late")
        ctx_hz = None
        if win is not None:
            try:
                ctx_hz = win.context.rf_frequency_hz
            except vrt.VrtError:
                ctx_hz = int(win.context.rf_frequency_hz_float)
                reasons.append("fractional_context_frequency")
            if abs(ctx_hz - edge.commits_hz) > freq_tolerance_hz:
                reasons.append("context_frequency_mismatch")
            if edge.readback_hz is not None and abs(edge.readback_hz - ctx_hz) > freq_tolerance_hz:
                reasons.append("readback_mismatch")
        elif edge.executed and reasons == ["no_context"]:
            # An untagged window may still exist for this hop; account its data but never label it.
            orphan = next((w for w in windows if w.context is None and w.packets), None)
            if orphan is not None:
                win = orphan
                windows.remove(orphan)
        samples = win.samples if win else 0
        gaps = win.gaps if win else 0
        n_pkts = len(win.packets) if win else 0
        expected_pkts = -(-expected_samples // samples_per_packet)
        complete = (win is not None and win.context is not None and gaps == 0
                    and abs(samples - expected_samples) <= samples_per_packet)
        if win is not None and win.context is not None and not complete:
            reasons.append("incomplete")
        if not reasons:
            disposition = "ok"
        elif reasons == ["incomplete"]:
            disposition = "incomplete"
        else:
            disposition = "invalid"
        last_ps = None
        if win and win.packets:
            lp = win.packets[-1][1]
            last_ps = lp.int_secs * PS + lp.frac_ps + (lp.payload_words - 1) * PS // sample_rate_hz
        hops.append(CaptureRecord(
            index=e.index, priming=e.priming, slot=e.slot, planned_hz=e.commits_hz,
            readback_hz=edge.readback_hz, context_hz=ctx_hz,
            epoch=win.context.hop_epoch if (win and win.context) else None,
            tick_requested=edge.requested_ticks, tick_executed=edge.executed_ticks, tick_status=edge.status,
            stage_late=edge.stage_late,
            context_mono_ns=win.context_record.mono_ns if (win and win.context_record) else None,
            first_sample_ps=win.first_sample_ps if win else None, last_sample_ps=last_ps,
            first_arrival_mono_ns=win.packets[0][0].mono_ns if (win and win.packets) else None,
            packets=n_pkts, samples=samples, expected_samples=expected_samples, gaps=gaps,
            lost_packets_estimate=max(0, expected_pkts - n_pkts) if (win and win.context) else 0,
            complete=complete, disposition=disposition, reasons=reasons))
    counts.orphan_contexts += len(by_epoch)
    overall = "ok"
    if any(h.disposition == "invalid" for h in hops):
        overall = "invalid"
    elif any(h.disposition == "incomplete" for h in hops):
        overall = "incomplete"
    return AssociationResult(hops=hops, counts=counts, disposition=overall, epochs_seen=epochs_seen)
