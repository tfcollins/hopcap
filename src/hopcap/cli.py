# SPDX-License-Identifier: MIT
"""hopcap command line.

``hopcap plan --plan FILE --dry-run`` compiles a plan offline and prints JSON: slot table,
edges with their Tick descriptors as the 12 hex words the driver accepts, and the timing
checks.  Live commands (``run``) live in :mod:`hopcap.run` and are wired in later phases.
``hopcap settle RUN_DIR --ref-hz F`` measures hop settle from a fixed RF reference
(:mod:`hopcap.settle`).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from tick.ffh import descriptor_csv

from hopcap.descriptors import descriptors_for_edge
from hopcap.plan import HopPlan, PlanError, compile_plan

DEFAULT_TICK_HZ = 100_000_000
DEFAULT_FS_HZ = 245_760_000


def load_plan(path: Path, *, tick_hz: int = DEFAULT_TICK_HZ) -> tuple[HopPlan, int]:
    """Plan JSON uses human units (µs) or samples; everything becomes ticks here."""
    raw = json.loads(Path(path).read_text())
    fs = int(raw.get("sample_rate_hz", DEFAULT_FS_HZ))

    def ticks(key_us: str, key_ticks: str, default: int | None = None) -> int:
        if key_ticks in raw:
            return int(raw[key_ticks])
        if key_us in raw:
            return HopPlan.ticks_for_seconds(float(raw[key_us]) * 1e-6, tick_hz=tick_hz)
        if default is None:
            raise PlanError(f"plan needs {key_us} or {key_ticks}")
        return default

    if "capture_samples" in raw:
        capture = HopPlan.ticks_for_samples(
            int(raw["capture_samples"]), sample_rate_hz=fs, tick_hz=tick_hz
        )
    else:
        capture = ticks("capture_us", "capture_ticks")
    kw = dict(
        frequencies_hz=tuple(int(f) for f in raw["frequencies_hz"]),
        period_ticks=ticks("period_us", "period_ticks"),
        settle_ticks=ticks("settle_us", "settle_ticks"),
        capture_ticks=capture,
        pulse_ticks=ticks("pulse_us", "pulse_ticks", HopPlan.pulse_ticks),
        lead_ticks=ticks("lead_us", "lead_ticks", HopPlan.lead_ticks),
        name=str(raw.get("name", Path(path).stem)),
    )
    if "init_frequency_hz" in raw:
        kw["init_frequency_hz"] = int(raw["init_frequency_hz"])
    if "band_hz" in raw:
        kw["band_hz"] = (int(raw["band_hz"][0]), int(raw["band_hz"][1]))
    return HopPlan(**kw), fs


def dry_run(plan: HopPlan, *, fs: int, t0_ticks: int, tick_hz: int) -> dict:
    c = compile_plan(plan, t0_ticks=t0_ticks, tick_hz=tick_hz)
    edges = []
    for e in c.edges:
        rise, fall = descriptors_for_edge(e)
        edges.append(
            {
                **asdict(e),
                "rise_sequence_id": rise.sequence_id,
                "fall_sequence_id": fall.sequence_id,
                "rise_descriptor_hex": descriptor_csv(rise),
                "fall_descriptor_hex": descriptor_csv(fall),
                "capture_open_ticks": e.rise_ticks + plan.settle_ticks,
                "capture_close_ticks": e.rise_ticks + plan.settle_ticks + plan.capture_ticks,
            }
        )
    busy = plan.settle_ticks + plan.capture_ticks + plan.pulse_ticks + plan.context_margin_ticks
    return {
        "schema": "hopcap-dry-run/v1",
        "plan": {
            **asdict(plan),
            "frequencies_hz": list(plan.frequencies_hz),
            "band_hz": list(plan.band_hz),
        },
        "tick_hz": tick_hz,
        "t0_ticks": t0_ticks,
        "sample_rate_hz": fs,
        "capture_samples": plan.capture_samples(sample_rate_hz=fs, tick_hz=tick_hz),
        "slots": {
            "frequencies_hz": list(c.slots.frequencies_hz),
            "words_q44_20": [str(w) for w in c.slots.words()],
        },
        "edges": edges,
        "timing": {
            "ok": True,
            "busy_ticks": busy,
            "period_ticks": plan.period_ticks,
            "slack_ticks": plan.period_ticks - busy,
            "end_ticks": c.end_ticks,
            "duration_s": (c.end_ticks - t0_ticks) / tick_hz,
        },
        "pipeline_note": "edge k commits the frequency staged before edge k-1; edge 0 primes on init_frequency_hz",
    }


def _apply_overrides(plan: HopPlan, args, *, fs: int, tick_hz: int) -> HopPlan:
    from dataclasses import replace

    kw = {}
    if getattr(args, "settle_us", None) is not None:
        kw["settle_ticks"] = HopPlan.ticks_for_seconds(args.settle_us * 1e-6, tick_hz=tick_hz)
    if getattr(args, "capture_us", None) is not None:
        kw["capture_ticks"] = HopPlan.ticks_for_seconds(args.capture_us * 1e-6, tick_hz=tick_hz)
    if getattr(args, "capture_samples", None) is not None:
        kw["capture_ticks"] = HopPlan.ticks_for_samples(
            args.capture_samples, sample_rate_hz=fs, tick_hz=tick_hz
        )
    if getattr(args, "period_us", None) is not None:
        kw["period_ticks"] = HopPlan.ticks_for_seconds(args.period_us * 1e-6, tick_hz=tick_hz)
    return replace(plan, **kw) if kw else plan


def cmd_run(args) -> int:
    from hopcap import framer as framer_mod
    from hopcap.run import RunConfig, build_components_from_board, run_live
    from hopcap.transport import SshShell

    plan, fs = load_plan(args.plan, tick_hz=args.tick_hz)
    plan = _apply_overrides(plan, args, fs=fs, tick_hz=args.tick_hz)
    host, port = args.bind.rsplit(":", 1)
    from hopcap import link

    dest_mac = args.dest_mac or link.receiver().mac
    args.rx_interface = args.rx_interface or link.receiver().interface
    fcfg = framer_mod.FramerConfig(dest_ip=host, dest_port=int(port), dest_mac=dest_mac)
    board_ip = (
        args.iio_uri.split(":", 1)[1]
        if args.iio_uri.startswith("ip:")
        else args.target.split("@")[-1]
    )
    if args.dry_run:
        out = dry_run(plan, fs=fs, t0_ticks=0, tick_hz=args.tick_hz)
        out["endpoints"] = {
            "tick": {"uri": args.iio_uri, "device": "axi-timed-command-scheduler"},
            "talise": {"uri": args.iio_uri, "device": "adrv9009-phy"},
            "hopctrl": {"target": args.target, "base": f"0x{args.hopctrl_base:08x}"},
            "framer": {"target": args.target, "device": "axi-vrt49-framer"},
            "receiver": {"bind": args.bind, "interface": args.rx_interface, "kind": args.receiver},
        }
        out["framer"] = asdict(fcfg)
        out["out_dir"] = str(args.out)
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    shell = SshShell(args.target)
    comps = build_components_from_board(
        shell,
        board_ip=board_ip,
        out_dir=args.out,
        bind=args.bind,
        rx_interface=args.rx_interface,
        rx_cpu=args.rx_cpu,
        hopctrl_base=args.hopctrl_base,
        iio_uri=args.iio_uri,
        receiver=args.receiver,
    )
    cfg = RunConfig(
        plan=plan,
        out_dir=args.out,
        sample_rate_hz=fs,
        framer=fcfg,
        provenance={"hopcap_commit": _git_sha(), "iio_uri": args.iio_uri, "target": args.target},
        expected_peer=(fcfg.source_ip, fcfg.source_port),
        lookahead_hops=args.lookahead,
    )
    report = run_live(cfg, comps)
    print(f"hopcap run: {report.disposition} -> {report.out_dir}")
    return 0 if report.disposition == "ok" else 1


def cmd_decode(args) -> int:
    from hopcap.associate import EdgeResult, associate
    from hopcap.export import write_bundle
    from hopcap.spool import iter_records

    run = json.loads(Path(args.run).read_text())
    pl = run["plan"]
    plan = HopPlan(
        frequencies_hz=tuple(pl["frequencies_hz"]),
        period_ticks=pl["period_ticks"],
        settle_ticks=pl["settle_ticks"],
        capture_ticks=pl["capture_ticks"],
        pulse_ticks=pl.get("pulse_ticks", HopPlan.pulse_ticks),
        lead_ticks=pl.get("lead_ticks", HopPlan.lead_ticks),
        init_frequency_hz=pl.get("init_frequency_hz", HopPlan.init_frequency_hz),
        name=pl.get("name", "decoded"),
    )
    compiled = compile_plan(plan, t0_ticks=run["t0_ticks"], tick_hz=run["tick_hz"])
    edges = [
        EdgeResult(
            **{
                k: e[k]
                for k in (
                    "index",
                    "slot",
                    "commits_hz",
                    "requested_ticks",
                    "executed_ticks",
                    "status",
                    "readback_hz",
                    "stage_late",
                )
            }
        )
        for e in run["edges"]
    ]
    with Path(args.spool).open("rb") as fp:
        records = list(iter_records(fp, strict=False))
    result = associate(
        records,
        compiled,
        edges,
        sample_rate_hz=run["sample_rate_hz"],
        samples_per_packet=run["samples_per_packet"],
        epoch_base=run.get("epoch_base", 0),
        expected_peer=tuple(run["expected_peer"]) if run.get("expected_peer") else None,
    )
    write_bundle(
        Path(args.out),
        compiled=compiled,
        result=result,
        records=records,
        sample_rate_hz=run["sample_rate_hz"],
        provenance=run.get("provenance", {"decoded_from": str(args.spool)}),
        counters=run.get("counters", {}),
    )
    print(f"hopcap decode: {result.disposition} -> {args.out}")
    return 0 if result.disposition != "invalid" else 1


def cmd_verify(args) -> int:
    import subprocess

    script = Path(__file__).resolve().parents[2] / "scripts" / "verify_evidence.py"
    return subprocess.run([sys.executable, str(script), args.kind, str(args.dir)]).returncode


def _us(v: float | None) -> str:
    return "-" if v is None else f"{v * 1e6:.2f}"


def cmd_settle(args) -> int:
    from hopcap.settle import analyse_run

    out = analyse_run(
        list(args.run_dir),
        args.ref_hz,
        conjugate=args.conjugate,
        tol_hz=args.tol_hz,
        phase_tol_deg=args.phase_tol_deg,
        window=args.window,
        tail_fraction=args.tail_fraction,
        min_snr_db=args.min_snr_db,
    )
    s = out.summary
    if args.json:
        json.dump(out.as_dict(), sys.stdout, indent=2, allow_nan=False)
        sys.stdout.write("\n")
    else:
        print(
            f"hopcap settle: reference {args.ref_hz / 1e6:.6f} MHz, tol {args.tol_hz:g} Hz, "
            f"phase tol {args.phase_tol_deg:g} deg; times from the first captured sample"
        )
        hdr = (
            "hop",
            "prim",
            "LO MHz",
            "offset MHz",
            "SNR dB",
            "settle us",
            "phase us",
            "err Hz",
            "status",
        )
        print("{:>4} {:>4} {:>10} {:>10} {:>7} {:>10} {:>10} {:>9}  {}".format(*hdr))
        for h in out.hops:
            r = h.result
            status = "ok" if r.ok else r.reason
            if r.ok and r.settled_at_first_sample:
                status = "ok (settled at first sample: bound only)"
            print(
                "{:>4} {:>4} {:>10.3f} {:>+10.3f} {:>7} {:>10} {:>10} {:>9}  {}".format(
                    h.index,
                    "yes" if h.priming else "",
                    h.planned_hz / 1e6,
                    r.expected_offset_hz / 1e6,
                    "-" if r.tone_snr_db is None else f"{r.tone_snr_db:.1f}",
                    _us(r.settle_s),
                    _us(r.phase_settle_s),
                    "-" if r.offset_error_hz is None else f"{r.offset_error_hz:+.1f}",
                    status,
                )
            )
        st, ph = s["settle_s"], s["phase_settle_s"]
        print(
            f"non-priming ok: {s['non_priming_ok']} of {sum(not h.priming for h in out.hops)} hops"
        )
        if st:
            print(f"settle  median {_us(st['median'])} us  max {_us(st['max'])} us")
            print(f"phase   median {_us(ph['median'])} us  max {_us(ph['max'])} us")
        for p in s["priming"]:
            print(
                f"priming hop {p['index']}: "
                + (
                    f"settle {_us(p['settle_s'])} us, phase {_us(p['phase_settle_s'])} us"
                    if p["ok"]
                    else str(p["reason"])
                )
            )
        print("note: " + s["time_reference"])
    return 0 if s["non_priming_ok"] else 1


def _git_sha() -> str:
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hopcap")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="compile a plan; --dry-run prints JSON and touches no hardware")
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--t0-ticks", type=int, default=0)
    p.add_argument("--tick-hz", type=int, default=DEFAULT_TICK_HZ)
    p.add_argument("--dry-run", action="store_true")

    r = sub.add_parser(
        "run", help="live run against tron (or --dry-run to resolve everything offline)"
    )
    r.add_argument("--plan", required=True, type=Path)
    r.add_argument("--iio-uri", required=True, help="e.g. ip:10.0.0.79")
    r.add_argument("--target", required=True, help="ssh target, e.g. root@10.0.0.79")
    r.add_argument("--bind", default="192.0.2.2:4991")
    r.add_argument(
        "--rx-interface",
        default=None,
        help="receiver NIC (default: nemo's NIC for VRT49_PHYSICAL_PORT, see hopcap.link)",
    )
    r.add_argument("--rx-cpu", type=int)
    r.add_argument(
        "--dest-mac",
        default=None,
        help="receiver MAC (default: nemo's NIC for VRT49_PHYSICAL_PORT, see hopcap.link)",
    )
    r.add_argument("--hopctrl-base", type=lambda v: int(v, 0), default=0x84AC0000)
    r.add_argument("--receiver", choices=("c", "python"), default="c")
    r.add_argument("--lookahead", type=int, default=4)
    r.add_argument("--tick-hz", type=int, default=DEFAULT_TICK_HZ)
    r.add_argument("--settle-us", type=float)
    r.add_argument("--capture-us", type=float)
    r.add_argument("--capture-samples", type=int)
    r.add_argument("--period-us", type=float)
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--dry-run", action="store_true")

    d = sub.add_parser(
        "decode", help="rebuild a bundle from a spool and the run's edge results JSON"
    )
    d.add_argument("--spool", required=True, type=Path)
    d.add_argument(
        "--run",
        required=True,
        type=Path,
        help="JSON: plan, t0_ticks, tick_hz, sample_rate_hz, samples_per_packet, edges",
    )
    d.add_argument("--out", required=True, type=Path)

    v = sub.add_parser("verify", help="verify an evidence directory")
    v.add_argument("kind", choices=("bitstream", "boot", "run"))
    v.add_argument("dir", type=Path)

    st = sub.add_parser(
        "settle", help="measure hop settle from a fixed RF reference tone (settle_us 0 runs)"
    )
    st.add_argument("run_dir", type=Path, nargs="+", help="bundle(s): manifest.json + hops/")
    st.add_argument("--ref-hz", type=float, required=True, help="reference CW frequency, Hz")
    st.add_argument("--tol-hz", type=float, default=1000.0)
    st.add_argument("--phase-tol-deg", type=float, default=5.0)
    st.add_argument("--window", type=int, default=256, help="smoothing window, samples")
    st.add_argument("--tail-fraction", type=float, default=0.25)
    st.add_argument("--min-snr-db", type=float, default=10.0)
    st.add_argument("--conjugate", action="store_true", help="flip the spectrum (I/Q swapped)")
    st.add_argument("--json", action="store_true", help="print the full analysis as JSON")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            plan, fs = load_plan(args.plan, tick_hz=args.tick_hz)
            out = dry_run(plan, fs=fs, t0_ticks=args.t0_ticks, tick_hz=args.tick_hz)
            json.dump(out, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0
        if args.cmd == "run":
            return cmd_run(args)
        if args.cmd == "decode":
            return cmd_decode(args)
        if args.cmd == "verify":
            return cmd_verify(args)
        if args.cmd == "settle":
            return cmd_settle(args)
    except PlanError as exc:
        print(f"hopcap: plan error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
