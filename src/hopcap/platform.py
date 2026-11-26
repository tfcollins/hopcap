# SPDX-License-Identifier: MIT
"""Per-platform constants: the one place that knows which board hopcap is driving.

Two platforms share the host code:

* ``zu11eg`` — ADRV9009-ZU11EG on place ``tron`` (the original hopcap). Tick and the hop
  controller both run at 100 MHz, Tick ``gpio_out[0]`` is the FHM trigger and the controller's
  event, ``gpio_out[3:1]`` the slot; the Talise FHM engine is one hop behind, so the host stages.
* ``adrv9361`` — ADRV9361-Z7035 on place ``lablp`` (spec
  ``docs/superpowers/specs/2026-11-25-adrv9361z7035-hop-design.md``). Tick runs at **50 MHz**
  (it misses 100 MHz on the Z7035 -2L), the controller at 100 MHz. Tick ``gpio_out[2:0]`` drives
  AD9361 CTRL_IN[3:1] (FastLock profile k for RX and TX together) and ``gpio_out[3]`` is the
  event; nothing is staged.

Units: plans are written in microseconds. Tick ticks = µs * TickHz / 1e6 (:func:`us_to_ticks`);
the controller's SETTLE / CAPTURE / DRAIN_TIMEOUT count its own clock (``clock_hz`` in
``hdl/hopctrl_regmap.json``), converted from Tick ticks with :func:`controller_cycles`. Never
write a Tick count into a controller register or the other way round.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hopcap import link

REGMAP_PATH = Path(__file__).resolve().parents[2] / "hdl" / "hopctrl_regmap.json"
CONTROLLER_HZ = int(json.loads(REGMAP_PATH.read_text())["clock_hz"])  # sys_cpu_clk, 100 MHz


class PlatformError(ValueError):
    pass


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------


def us_to_ticks(us: float, tick_hz: int) -> int:
    """Microseconds -> clock cycles at ``tick_hz``, rounded to the nearest cycle."""
    return round(us * tick_hz / 1_000_000)


def ticks_to_us(ticks: int, tick_hz: int) -> float:
    return ticks * 1_000_000 / tick_hz


def controller_cycles(ticks: int, *, tick_hz: int, controller_hz: int = CONTROLLER_HZ) -> int:
    """A duration in Tick ticks -> hop-controller cycles, rounded up (a settle or capture is
    never shortened). The identity when Tick runs on the controller clock (ZU11EG, 100 MHz);
    x2 on the ADRV9361 (Tick 50 MHz)."""
    if ticks < 0:
        raise PlatformError(f"negative duration {ticks} ticks")
    if tick_hz <= 0:
        raise PlatformError(f"tick_hz must be > 0, got {tick_hz}")
    return -(-ticks * controller_hz // tick_hz)


# ---------------------------------------------------------------------------
# Tick gpio_out bit map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpioBitMap:
    """Where the event and the 3-bit slot sit on Tick ``gpio_out[3:0]``.

    A hop to slot k is two GPIO_WRITEs: a rise ``mask 0xF`` that writes the slot and sets the
    event bit on the same tick, and a fall ``pulse`` later that clears only the event bit.
    """

    name: str
    event_mask: int
    slot_shift: int
    gpio_mask: int = 0xF

    @property
    def slot_mask(self) -> int:
        return 0x7 << self.slot_shift

    def value(self, slot: int, event: bool) -> int:
        if not 0 <= slot <= 7:
            raise ValueError(f"slot {slot} out of range 0..7")
        return (slot << self.slot_shift) | (self.event_mask if event else 0)

    def rise(self, slot: int) -> tuple[int, int]:
        return self.gpio_mask, self.value(slot, True)

    def fall(self) -> tuple[int, int]:
        return self.event_mask, 0

    def park(self) -> tuple[int, int]:
        """All four bits low: event low and slot / CTRL_IN code 0."""
        return self.gpio_mask, self.value(0, False)


# ZU11EG: gpio_out[0] = TAL_GPIO_0 trigger + controller event, gpio_out[3:1] = slot.
ZU11EG_BITS = GpioBitMap("zu11eg", event_mask=0x1, slot_shift=1)
# ADRV9361: gpio_out[2:0] = AD9361 CTRL_IN[3:1] (FastLock profile), gpio_out[3] = event. The BD
# feeds the controller {gpio_out[2:0], gpio_out[3]}, so it still sees event = bit 0.
ADRV9361_BITS = GpioBitMap("adrv9361", event_mask=0x8, slot_shift=0)


# ---------------------------------------------------------------------------
# platforms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Platform:
    name: str
    place: str
    part: str  # adi-lg --part
    carrier: str  # adi-lg --carrier
    tick_hz: int  # expected Tick TIME_TICK_HZ, checked against the device at runtime
    controller_hz: int
    bits: GpioBitMap
    hop_mode: str  # "talise-fhm" (one-behind staging) | "fastlock" (pin-selected profiles)
    phy_iio_name: str
    rx_iio_name: str | None  # the ADC core whose buffer feeds the VRT tap (adrv9361)
    dds_iio_name: str | None  # the DDS core that transmits the loopback tone (adrv9361)
    hopctrl_base: int
    framer_base: int | None  # framer AXI-Lite (MMIO-only registers: TB_PPS_PERIOD, PCS status)
    sample_rate_hz: int
    packet_mtu: int
    pps_sources: tuple[str, ...]  # framer adi,pps-source-names, in register order
    pps_source: str  # the one hopcap selects
    source_ip: str  # framer UDP source (the board's end of the data link)
    source_port: int
    source_mac: str
    dest_port: int
    max_slots: int = 8
    # Fail a run whose Tick is not at tick_hz. Off on the ZU11EG: the same board also runs the
    # timed-dma image with Tick at 122.88 MHz, and every ZU11EG conversion uses the probed rate.
    tick_hz_strict: bool = True
    tick_iio_name: str = "axi-timed-command-scheduler"
    framer_iio_name: str = "vrt49_framer"  # iio-vrt49-buffer.c indio_dev->name
    receiver_port: str | None = None  # fixed receiver; None = VRT49_PHYSICAL_PORT (hopcap.link)

    def receiver(self, port: str | None = None) -> link.Receiver:
        if self.receiver_port is not None:
            return link.platform_receiver(self.name)
        return link.receiver(port)

    def check_tick_hz(self, probed: int) -> int:
        """Fail closed when the device's TIME_TICK_HZ is not this platform's Tick clock: every
        Tick timestamp the host computes would be off by the ratio."""
        if int(probed) != self.tick_hz:
            raise PlatformError(
                f"{self.name}: Tick reports time_tick_hz {probed}, expected {self.tick_hz} "
                "(wrong bitstream, or a Tick clock that is not this platform's)"
            )
        return int(probed)


ZU11EG = Platform(
    name="zu11eg",
    place="tron",
    part="adrv9009zu11eg",
    carrier="adrv2crr-fmc",
    tick_hz=100_000_000,
    controller_hz=CONTROLLER_HZ,
    bits=ZU11EG_BITS,
    hop_mode="talise-fhm",
    phy_iio_name="adrv9009-phy",
    rx_iio_name=None,
    dds_iio_name=None,
    hopctrl_base=0x84AC0000,
    framer_base=None,
    sample_rate_hz=245_760_000,
    packet_mtu=1468,
    pps_sources=("corundum", "external"),
    pps_source="corundum",
    source_ip="192.0.2.1",
    source_port=4991,
    source_mac="02:00:00:00:00:01",
    dest_port=4991,
    tick_hz_strict=False,
)

ADRV9361 = Platform(
    name="adrv9361",
    place="lablp",
    part="adrv9361z7035",
    carrier="adrv1crr-fmc",
    tick_hz=50_000_000,
    controller_hz=CONTROLLER_HZ,
    bits=ADRV9361_BITS,
    hop_mode="fastlock",
    phy_iio_name="ad9361-phy",
    rx_iio_name="cf-ad9361-lpc",
    dds_iio_name="cf-ad9361-dds-core-lpc",
    hopctrl_base=0x44AB0000,
    framer_base=0x44A80000,
    sample_rate_hz=15_360_000,
    packet_mtu=1024,  # overlay adi,packet-mtu: 249 CI16 samples per packet
    pps_sources=("tick", "disabled"),  # kernel/dts/zynq-adrv9361z7035-hopcap-overlay.dts
    pps_source="tick",
    source_ip="192.168.2.211",  # vrt49 adrv9361-sfp-1g.md board source on nemo's 192.168.2.0/24
    source_port=4991,
    source_mac="02:00:00:00:00:01",
    dest_port=4991,
    receiver_port="sfp",
)

PLATFORMS = {p.name: p for p in (ZU11EG, ADRV9361)}
DEFAULT = ZU11EG
ENV = "HOPCAP_PLATFORM"


def get(name: str | None = None) -> Platform:
    name = name or DEFAULT.name
    try:
        return PLATFORMS[name]
    except KeyError:
        raise PlatformError(f"unknown platform {name!r}: one of {', '.join(PLATFORMS)}") from None


def from_env(env=None) -> Platform:
    import os

    return get(((os.environ if env is None else env).get(ENV) or "").strip() or None)
