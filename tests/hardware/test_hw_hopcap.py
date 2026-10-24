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
    sums = board["shell"].run("sha256sum /boot/BOOT.BIN /boot/Image /boot/system.dtb")
    for name, (_url, sha) in board["artifacts"].staged_names().items():
        assert sha in sums, f"{name} on /boot is not the staged artifact"
    assert "vrt49_framer" in board["shell"].run("cat /sys/bus/iio/devices/*/name")
    assert "adrv9009-phy" in board["shell"].run("cat /sys/bus/iio/devices/*/name")
    assert "axi-timed-command-scheduler" in board["shell"].run("cat /sys/bus/iio/devices/*/name")


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
    # report-only
    print(
        json.dumps(
            {
                "host_rx": man["counters"].get("host_after"),
                "incomplete": [h["index"] for h in hops if not h["complete"]],
                "stage_late": [h["index"] for h in hops if h["stage_late"]],
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
