# SPDX-License-Identifier: MIT
"""Talise FHM staging over IIO channel attributes (adrv9009-phy TRX LO channel).

Mirrors tick/examples/adrv9009_fhm_hop_demo.py: ``frequency_hopping_mode_enable`` arms GPIO
trigger mode with the driver's defaults (TAL_GPIO_0, fhmInitFrequency), writing
``frequency_hopping_mode`` stages the *next* frequency, reading it returns the live RF PLL
frequency.  The one-behind pipeline is modelled in :class:`hopcap.plan.FhmPipelineModel`,
not here.
"""

from __future__ import annotations

from collections.abc import Callable

ReadAttr = Callable[[str], str]
WriteAttr = Callable[[str, str], None]

ATTR_ENABLE = "frequency_hopping_mode_enable"
ATTR_HOP = "frequency_hopping_mode"


class TaliseError(RuntimeError):
    pass


class FhmStager:
    def __init__(
        self,
        read_attr: ReadAttr,
        write_attr: WriteAttr,
        *,
        band_hz: tuple[int, int] = (2_400_000_000, 2_500_000_000),
    ):
        self.read_attr = read_attr
        self.write_attr = write_attr
        self.band_hz = band_hz
        self.staged_hz: int | None = None
        self.enabled = False

    def enable(self) -> None:
        self.write_attr(ATTR_ENABLE, "1")
        self.enabled = True

    def disable(self) -> None:
        self.write_attr(ATTR_ENABLE, "0")
        self.enabled = False

    def stage(self, hz: int) -> None:
        lo, hi = self.band_hz
        if not lo <= hz <= hi:
            raise TaliseError(f"{hz} Hz outside FHM band {lo}..{hi}")
        self.write_attr(ATTR_HOP, str(int(hz)))
        self.staged_hz = int(hz)

    def readback_hz(self) -> int:
        raw = self.read_attr(ATTR_HOP)
        try:
            return int(str(raw).strip().split()[-1], 0)
        except (ValueError, IndexError) as exc:
            raise TaliseError(f"unparseable {ATTR_HOP} readback {raw!r}") from exc
