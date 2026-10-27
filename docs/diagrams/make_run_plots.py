#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Render the example page's plots from the committed run data (docs/data/run7) into
docs/_static/run7/*.svg.  Regenerate with `python3 docs/diagrams/make_run_plots.py`; CI
fails if the SVGs drift from the data.

Palette: the dataviz reference instance (validated 3-slot categorical: blue, orange, aqua),
one hue for magnitude, status gray for "lost". Text wears text tokens, marks are thin,
grid is recessive, every multi-series panel has a legend and direct labels.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "data" / "run7"
OUT = ROOT / "docs" / "_static" / "run7"

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
GRAY, GRID, TEXT, TEXT2 = "#b9b8b1", "#e6e5e0", "#0b0b0b", "#52514e"
SURFACE = "#fcfcfb"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID,
        "axes.labelcolor": TEXT2,
        "xtick.color": TEXT2,
        "ytick.color": TEXT2,
        "text.color": TEXT,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "grid.linestyle": "-",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "font.size": 9,
        "axes.titlesize": 10,
        "axes.titleweight": "normal",
        "legend.frameon": False,
        "lines.linewidth": 1.6,
        "svg.fonttype": "none",
        "svg.hashsalt": "hopcap-run7",  # deterministic element ids: SVGs reproducible byte for byte
    }
)


def load(name: str):
    return json.loads((DATA / name).read_text())


def savefig(fig, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.svg", metadata={"Date": None})
    fig.savefig(OUT / f"{name}.png", dpi=110)  # preview only (gitignored)
    plt.close(fig)


def plot_frequency_per_hop(m: dict) -> None:
    hops = m["hops"]
    x = np.arange(len(hops))
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    ax.plot(
        x,
        [h["planned_hz"] / 1e6 for h in hops],
        color=GRAY,
        lw=1.2,
        zorder=1,
        label="planned (slot word)",
    )
    ax.scatter(
        x,
        [h["readback_hz"] / 1e6 for h in hops],
        s=120,
        facecolors="none",
        edgecolors=BLUE,
        lw=1.8,
        zorder=3,
        label="Talise readback after the edge",
    )
    ax.scatter(
        x,
        [h["context_hz"] / 1e6 for h in hops],
        s=28,
        color=ORANGE,
        zorder=4,
        label="Context RF frequency (hardware)",
    )
    for i, h in enumerate(hops):
        ax.annotate(
            f"{h['context_hz'] / 1e6:.0f} MHz\nepoch {h['epoch']}",
            (i, h["context_hz"] / 1e6),
            textcoords="offset points",
            xytext=(0, 12),
            ha="center",
            fontsize=8,
            color=TEXT2,
        )
    ax.set_xticks(x, [f"edge {h['index']}" + (" (priming)" if h["priming"] else "") for h in hops])
    ax.set_ylabel("RF frequency (MHz)")
    ax.set_ylim(2395, 2510)
    ax.set_title(
        "Every hop: planned slot, Talise PLL readback and the Context the FPGA emitted agree"
    )
    ax.legend(loc="lower right", fontsize=8)
    fig.tight_layout()
    savefig(fig, "frequency_per_hop")


def plot_packets_per_hop(m: dict) -> None:
    hops = m["hops"]
    expected = -(-hops[0]["expected_samples"] // 360)
    x = np.arange(len(hops))
    got = np.array([h["packets"] for h in hops])
    lost = np.clip(expected - got, 0, None)
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    ax.bar(x, got, width=0.55, color=BLUE, label="IF Data packets spooled")
    ax.bar(x, lost, width=0.55, bottom=got + 4, color=GRAY, label=f"missing vs {expected} expected")
    for i, (g, miss) in enumerate(zip(got, lost, strict=True)):
        ax.text(i, g - 30, f"{g}", ha="center", va="top", color=SURFACE, fontsize=8)
        if miss:
            ax.text(i, g + miss + 14, f"-{miss}", ha="center", va="bottom", color=TEXT2, fontsize=8)
    ax.axhline(expected, color=TEXT2, lw=0.8)
    ax.text(
        len(hops) - 0.55,
        expected + 10,
        f"{expected} = 1 ms at 245.76 MSPS",
        ha="right",
        fontsize=8,
        color=TEXT2,
    )
    ax.set_xticks(x, [f"hop {h['index']}\n{h['disposition']}" for h in hops])
    ax.set_ylabel("packets")
    ax.set_ylim(0, expected * 1.18)
    rx_missed = m["counters"]["host_rx_missed_delta"]
    ax.set_title(
        f"Packets per capture: the FPGA sent 684 per hop with 0 drops; the host NIC ring missed {rx_missed}"
    )
    ax.legend(loc="upper left", fontsize=8, ncol=2)
    fig.tight_layout()
    savefig(fig, "packets_per_hop")


def plot_arrivals(arr: dict, m: dict) -> None:
    t = np.array(arr["t_us"]) / 1000.0  # ms since first datagram
    ctx = np.array(arr["is_context"], dtype=bool)
    epochs = np.array(arr["epoch"])
    fig, (ax, az) = plt.subplots(2, 1, figsize=(7.2, 4.6), gridspec_kw={"height_ratios": [1, 1.2]})
    # top: when did each burst arrive, and how big was it
    for tc, ep in zip(t[ctx], epochs[ctx], strict=True):
        # arrival stamps are per recvmmsg batch, so data can share the Context's timestamp: use >=
        n = int(((t >= tc) & (t < tc + 5) & ~ctx).sum())
        ax.bar(tc, n, width=6, color=BLUE, align="edge")
        ax.axvline(tc, color=ORANGE, lw=1.4)
        ax.text(tc + 8, n + 15, f"epoch {ep}: {n} pkts", fontsize=7.5, color=TEXT2)
    ax.set_ylim(0, 820)
    ax.set_ylabel("datagrams per burst")
    ax.set_xlabel("host arrival time (ms, CLOCK_MONOTONIC, since the first datagram)")
    ax.set_title(
        "Five bursts 200 ms apart, each announced by its Context; silence while the gate is shut"
    )
    # bottom: inside one burst at 20 µs resolution
    tc = t[ctx][2]
    sel = (t >= tc - 0.1) & (t <= tc + 1.6)
    bins = np.arange(tc - 0.1, tc + 1.6, 0.02)
    counts, _ = np.histogram(t[sel & ~ctx], bins=bins)
    az.step(
        (bins[:-1] - tc) * 1e3,
        counts,
        where="post",
        color=BLUE,
        lw=1.2,
        label="IF Data datagrams per 20 µs",
    )
    az.fill_between((bins[:-1] - tc) * 1e3, counts, step="post", color=BLUE, alpha=0.15, lw=0)
    az.axvline(0, color=ORANGE, lw=1.6, label="Context (epoch 3) arrival")
    az.set_xlabel("time relative to the Context arrival (µs)")
    az.set_ylabel("datagrams")
    az.set_title(
        "Inside the epoch-3 burst: the Context leads, then 683 datagrams in ~1 ms (8.4 Gb/s)"
    )
    az.legend(loc="upper right", fontsize=7.5)
    fig.tight_layout()
    savefig(fig, "arrivals")


def plot_spectra(psd: dict) -> None:
    keys = sorted(psd, key=int)
    fig, axes = plt.subplots(1, len(keys), figsize=(9.6, 2.9), sharey=True)
    for ax, k in zip(axes, keys, strict=True):
        p = psd[k]
        f = np.array(p["f_mhz"])
        db = np.array(p["db"])
        ax.plot(f, db, color=BLUE, lw=1.0)
        ax.set_title(f"hop {k}: {p['rf_hz'] / 1e6:.0f} MHz", fontsize=9)
        ax.set_xlim(f.min(), f.max())
        ax.set_xticks([-100, 0, 100])
        ax.grid(True, axis="y")
        pk = int(np.argmax(db))
        ax.annotate(
            f"{f[pk]:+.1f} MHz",
            (f[pk], db[pk]),
            textcoords="offset points",
            xytext=(-4, 4),
            ha="right",
            fontsize=7,
            color=TEXT2,
        )
    axes[0].set_ylabel("PSD (dB/Hz, full scale = 0 dB)")
    fig.supxlabel("baseband offset from the hop's RF centre (MHz)", fontsize=9, color=TEXT2)
    fig.suptitle(
        "Welch PSD of each captured hop (245.76 MSPS, 1024-point): the same +40 MHz tone at every RF centre",
        fontsize=9.5,
    )
    fig.tight_layout()
    savefig(fig, "spectra")


def plot_iq_excerpt(iq: dict) -> None:
    n = len(iq["i"])
    t_us = np.arange(n) / iq["fs"] * 1e6
    fig, ax = plt.subplots(figsize=(7.2, 2.9))
    ax.plot(t_us, iq["i"], color=BLUE, lw=1.0, label="I")
    ax.plot(t_us, iq["q"], color=ORANGE, lw=1.0, label="Q")
    ax.set_xlabel(f"time since the hop {iq['hop']} capture opened (µs)")
    ax.set_ylabel("CI16 sample")
    ax.set_title(
        f"First {n} samples of hop {iq['hop']} as received (little-endian CI16 from the framer payload)"
    )
    ax.legend(loc="upper center", fontsize=8, ncol=2, bbox_to_anchor=(0.5, -0.32))
    fig.tight_layout()
    savefig(fig, "iq_excerpt")


def plot_timeline(m: dict) -> None:
    """Tick-domain plan vs execution: full schedule on top, one hop zoomed below so the
    200 µs settle and 1 ms open window are visible."""
    hz = m["tick"]["time_tick_hz"]
    t0 = m["tick"]["t0_ticks"]
    plan = m["plan"]
    settle_ms = plan["settle_ticks"] / hz * 1e3
    open_ms = plan["capture_ticks"] / hz * 1e3
    fig, (ax, az) = plt.subplots(2, 1, figsize=(7.2, 4.4), gridspec_kw={"height_ratios": [1, 1.15]})
    for h in m["hops"]:
        rise = (h["tick_requested"] - t0) / hz * 1e3
        exe = (h["tick_executed"] - t0) / hz * 1e3
        ax.plot([rise, rise], [0.25, 0.7], color=BLUE, lw=1.6)
        ax.plot(exe, 0.74, marker="v", color=ORANGE, ms=6, lw=0)
        ax.text(
            rise,
            0.82,
            f"edge {h['index']}  {h['context_hz'] / 1e6:.0f} MHz",
            ha="center",
            fontsize=7.5,
            color=TEXT2,
        )
    ax.set_yticks([])
    ax.set_ylim(0, 1.0)
    ax.set_xlabel("Tick device time (ms since t0)")
    ax.set_title("Five edges, 200 ms apart; every one executed at its requested tick")
    zoom = m["hops"][2]
    r = (zoom["tick_requested"] - t0) / hz * 1e3
    az.axvspan(r, r + settle_ms, color=GRAY, alpha=0.7, lw=0)
    az.axvspan(r + settle_ms, r + settle_ms + open_ms, color=AQUA, alpha=0.85, lw=0)
    az.axvline(r, color=BLUE, lw=1.8)
    az.plot((zoom["tick_executed"] - t0) / hz * 1e3, 0.9, marker="v", color=ORANGE, ms=7, lw=0)
    az.text(
        r + settle_ms / 2,
        0.5,
        f"SETTLE\n{settle_ms * 1e3:.0f} µs",
        ha="center",
        va="center",
        fontsize=8,
        color=TEXT,
    )
    az.text(
        r + settle_ms + open_ms / 2,
        0.5,
        f"OPEN (capture) {open_ms:.0f} ms\n683 packets, {zoom['samples']} samples",
        ha="center",
        va="center",
        fontsize=8,
        color=TEXT,
    )
    az.text(
        r + settle_ms + open_ms + 0.05,
        0.5,
        "CLOSED until\nthe next edge",
        va="center",
        fontsize=8,
        color=TEXT2,
    )
    az.set_xlim(r - 0.2, r + settle_ms + open_ms + 0.9)
    az.set_yticks([])
    az.set_ylim(0, 1.0)
    az.set_xlabel(f"Tick device time around edge {zoom['index']} (ms)")
    az.set_title(
        f"Zoom on edge {zoom['index']} ({zoom['context_hz'] / 1e6:.0f} MHz): hard-timed edge, settle guard, gated capture"
    )
    az.plot([], [], color=BLUE, lw=1.8, label="Tick GPIO_WRITE (FHM trigger) requested")
    az.plot([], [], marker="v", color=ORANGE, lw=0, label="executed (0-tick skew)")
    az.legend(loc="upper left", fontsize=7.5, bbox_to_anchor=(0.0, -0.32), ncol=2)
    fig.tight_layout()
    savefig(fig, "timeline")


def main() -> None:
    m = load("manifest.json")
    plot_timeline(m)
    plot_frequency_per_hop(m)
    plot_packets_per_hop(m)
    plot_arrivals(load("arrivals.json"), m)
    plot_spectra(load("psd.json"))
    plot_iq_excerpt(load("iq_excerpt.json"))
    print("wrote", sorted(p.name for p in OUT.glob("*.svg")))


if __name__ == "__main__":
    main()
