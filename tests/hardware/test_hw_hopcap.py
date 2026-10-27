# SPDX-License-Identifier: MIT
"""hopcap on tron: boot identity, the hop/capture plan, and the no-edge negative control."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.hopcap_hardware, pytest.mark.iio_hardware(["adrv9009zu11eg"])]
MIN_COMPLETE_FRACTION = float(os.getenv("HOPCAP_MIN_COMPLETE_FRACTION", "0.95"))


def test_boot_and_identity(board, components):
    from hopcap.hopctrl import HopController
    from hopcap.scheduler import TickSession

    hc = HopController(components.mmio, components.regs)
    ident, _version = hc.identify()
    assert ident == "HOP2"
    info = TickSession(components.tick_read, components.tick_write).probe()
    assert (
        info["time_tick_hz"] == 100_000_000 and info["queue_depth"] == 16 and info["gpio_capable"]
    )
    from hopcap.lab.boot import staged_sums_command

    sums = board["shell"].run(staged_sums_command(board["artifacts"].staged_names()))
    for name, (_url, sha) in board["artifacts"].staged_names().items():
        assert sha in sums, f"{name} on /boot is not the staged artifact"
    assert "vrt49_framer" in board["shell"].run("cat /sys/bus/iio/devices/*/name")
    assert "adrv9009-phy" in board["shell"].run("cat /sys/bus/iio/devices/*/name")
    assert "axi-timed-command-scheduler" in board["shell"].run("cat /sys/bus/iio/devices/*/name")


def test_udp_path_board_to_host(board, components):
    """A probe datagram from the board's Corundum address to the receiver socket must arrive.
    Isolates host-side delivery (NIC, rp_filter, firewall) from the FPGA stream."""
    import time

    rx = components.receiver
    rx.start()
    try:
        host, port = rx.bind
        bind = f"{host}:{port}"
        board["shell"].run(
            'python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); '
            f"s.bind(('192.0.2.1', 4991)); [s.sendto(b'hopcap-probe', ('{host}', {port})) for _ in range(20)]\""
        )
        time.sleep(0.5)
    finally:
        summary = rx.stop()
    assert summary["spool_records"] >= 1, (
        f"no probe datagram reached {bind}: check nemo `nstat -az | grep -E 'ReversePathFilter|UdpNoPorts'`, "
        "rp_filter on the receiver NIC (needs 0 when 192.0.2.2 lives on another interface) and ufw"
    )


def test_hop_capture_plan(board, components):
    from hopcap.cli import load_plan
    from hopcap.run import RunConfig, run_live

    plan, fs = load_plan(
        Path(
            os.environ.get(
                "HOPCAP_PLAN",
                Path(__file__).resolve().parents[2] / "examples/plans/band-2400-2500-4slot.json",
            )
        )
    )
    cfg = RunConfig(
        plan=plan,
        out_dir=Path(components.out_dir),
        sample_rate_hz=fs,
        provenance={
            "hopcap_commit": os.environ.get("HOPCAP_COMMIT", "unknown"),
            "boot": {k: v for k, v in os.environ.items() if k.startswith("VRT49_CORUNDUM_")},
        },
    )
    report = run_live(cfg, components)
    man = report.manifest
    hops = man["hops"]
    # hard failures
    assert man["disposition"] in ("ok", "incomplete"), man["disposition"]
    assert not [h["index"] for h in hops if h["disposition"] == "invalid"], "invalid hops"
    assert man["tick"]["late_count_delta"] == 0 and man["tick"]["overflow_count_delta"] == 0
    for h in hops:
        assert h["tick_status"] == "EXECUTED", h
        assert h["tick_executed"] == h["tick_requested"], "non-zero Tick skew"
        assert h["readback_hz"] == h["planned_hz"], f"Talise readback mismatch on hop {h['index']}"
        if h["context_hz"] is not None:
            assert h["context_hz"] == h["readback_hz"]
    epochs = [h["epoch"] for h in hops if h["epoch"] is not None]
    assert epochs == sorted(epochs) and len(set(epochs)) == len(epochs)
    fr = man["counters"]["framer"]
    assert (
        fr["v49_tx_dropped_packets"] == 0
        and fr["v49_udp_drop_count"] == 0
        and fr["v49_udp_length_error_count"] == 0
    )
    assert man["counts"]["malformed"] == 0 and man["counts"]["foreign_peer"] == 0
    assert (
        man["hopctrl"]["after"]["superseded_count"] == 0
        and not man["hopctrl"]["after"]["drain_timeout_sticky"]
    )
    non_priming = [h for h in hops if not h["priming"]]
    complete = sum(1 for h in non_priming if h["complete"])
    assert complete >= MIN_COMPLETE_FRACTION * len(non_priming), (complete, len(non_priming))
    # VRT timestamps: armed and LOCKED before the stream, still LOCKED with no synthetic edge
    # and no resync after it, and every in-capture step sample-exact (boot-attempt3 stamped 0)
    tb = man["time_base"]
    armed, after, st = tb["armed"]["after_arm"], tb["after"], tb["stamps"]
    assert armed["lock_state"] == "locked", tb["armed"]
    assert after["lock_state"] == "locked" and after["missed"] == armed["missed"], after
    assert not after["resync_sticky"], after
    assert st["steps_checked"] > 0 and st["zero_stamps"] == 0, st
    assert st["step_errors"] == 0 and st["non_increasing"] == 0, st
    # report-only
    print(
        json.dumps(
            {
                "host_rx": man["counters"].get("host_after"),
                "incomplete": [h["index"] for h in hops if not h["complete"]],
                "stage_late": [h["index"] for h in hops if h["stage_late"]],
                "time_base_before_arm": tb["armed"]["before"],
                "pps_crossings": [
                    st["second_crossings"],
                    st["crossing_residual_ps_min"],
                    st["crossing_residual_ps_max"],
                ],
                "hop_spacing_residual_ps": tb["hop_spacing_residual_ps"],
            },
            indent=2,
        )
    )


def test_negative_no_edge_no_hop(board, components):
    """Stage a frequency with no Tick edge: the PLL must not move and no Context may be emitted."""
    from hopcap.hopctrl import HopController

    hc = HopController(components.mmio, components.regs)
    epoch_before = hc.status().epoch
    before = components.stager.readback_hz()
    components.stager.enable()
    try:
        components.stager.stage(2_420_000_000)
        import time

        time.sleep(0.2)
        assert components.stager.readback_hz() == before
    finally:
        components.stager.disable()
    assert hc.status().epoch == epoch_before
