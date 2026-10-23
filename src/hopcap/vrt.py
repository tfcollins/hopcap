# SPDX-License-Identifier: MIT
"""Minimal VITA 49.2 codec for the hopcap stream: IF Data (type 1) and Context (type 4).

Pure Python so the host runner has no dependency on ``libvrt49.so``; the unit tests
cross-check it byte-for-byte against libvrt49 when that library is built.  The CIF
field-size tables mirror ``third_party/vrt49/src/libvrt49/cif.c``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

PKT_IF_DATA_SID = 0x1
PKT_CONTEXT = 0x4
PROLOGUE_WORDS = 7
Q44_20_SHIFT = 20

DEFAULT_CLASS_OUI = 0x00A0C6
DEFAULT_INFO_CLASS = 0xADC0
DEFAULT_PACKET_CLASS_DATA = 0x0010
DEFAULT_PACKET_CLASS_CONTEXT = 0x00C0

CIF0_CHANGE = 31
CIF0_CIF1_ENABLE, CIF0_CIF2_ENABLE, CIF0_CIF3_ENABLE, CIF0_CIF7_ENABLE = 1, 2, 3, 7
CIF0_RF_REF_FREQ = 27
CIF1_DISCRETE_IO_32 = 6
CIF2_INFO_SOURCE_ID = 21

# Word counts per bit, index = bit number (from libvrt49 cif.c).  0 = flag/unsupported.
_CIF0_SIZES = (
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    1,
    13,
    13,
    11,
    11,
    2,
    1,
    2,
    1,
    1,
    2,
    2,
    1,
    1,
    1,
    2,
    2,
    2,
    2,
    2,
    1,
    0,
)
_CIF1_SIZES = (
    0,
    2,
    1,
    1,
    1,
    2,
    1,
    0,
    0,
    0,
    13,
    0,
    0,
    2,
    1,
    2,
    1,
    1,
    1,
    1,
    1,
    0,
    0,
    0,
    1,
    1,
    1,
    1,
    0,
    1,
    1,
    1,
)
_CIF2_SIZES = (
    0,
    0,
    0,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    4,
    1,
    4,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
)
_CIF3_SIZES = (
    0,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    1,
    1,
    0,
    0,
    2,
    2,
    2,
    2,
    2,
    2,
    2,
    2,
    0,
    0,
    2,
    2,
)
_SIZES = {0: _CIF0_SIZES, 1: _CIF1_SIZES, 2: _CIF2_SIZES, 3: _CIF3_SIZES}
_CIF0_VARIABLE = {9, 8}  # GPS ASCII, context association lists

_NAMES = {
    (0, 30): "reference_point_id",
    (0, 29): "bandwidth",
    (0, 28): "if_reference_frequency",
    (0, 27): "rf_reference_frequency",
    (0, 26): "rf_reference_frequency_offset",
    (0, 25): "if_band_offset",
    (0, 24): "reference_level",
    (0, 23): "gain",
    (0, 22): "over_range_count",
    (0, 21): "sample_rate",
    (0, 20): "timestamp_adjustment",
    (0, 19): "timestamp_calibration_time",
    (0, 18): "temperature",
    (0, 17): "device_id",
    (0, 16): "state_event",
    (0, 15): "signal_data_format",
    (0, 14): "gps",
    (0, 13): "ins",
    (0, 12): "ecef_ephemeris",
    (0, 11): "relative_ephemeris",
    (0, 10): "ephemeris_ref_id",
    (1, 6): "discrete_io_32",
    (2, 21): "info_source_id",
}


class VrtError(ValueError):
    pass


def cif0_field_words(bit: int) -> int:
    return _CIF0_SIZES[bit]


def cif_field_words(page: int, bit: int) -> int:
    return _SIZES.get(page, (0,) * 32)[bit]


@dataclass(frozen=True)
class Prologue:
    pkt_type: int
    class_id_present: bool
    trailer_present: bool
    tsm: int
    tsi: int
    tsf: int
    pkt_count: int
    pkt_size_words: int
    stream_id: int
    class_oui: int
    info_class_code: int
    packet_class_code: int
    int_secs: int
    frac_ps: int


@dataclass(frozen=True)
class IfData(Prologue):
    payload_bytes: bytes = b""

    @property
    def payload_words(self) -> int:
        return len(self.payload_bytes) // 4

    def iq(self) -> np.ndarray:
        """Decode the payload as little-endian CI16 (I, Q) pairs, shape (N, 2)."""
        return np.frombuffer(self.payload_bytes, dtype="<i2").reshape(-1, 2)


@dataclass(frozen=True)
class Context(Prologue):
    changed: bool = False
    cif: dict[int, int] = field(default_factory=dict)
    fields: dict[str, int | tuple[int, ...]] = field(default_factory=dict)

    @property
    def rf_frequency_q44_20(self) -> int:
        try:
            return int(self.fields["rf_reference_frequency"])
        except KeyError as exc:
            raise VrtError("Context carries no RF reference frequency") from exc

    @property
    def rf_frequency_hz(self) -> int:
        q = self.rf_frequency_q44_20
        if q & ((1 << Q44_20_SHIFT) - 1):
            raise VrtError(f"RF reference frequency 0x{q:x} has a fractional Q44.20 part")
        return q >> Q44_20_SHIFT

    @property
    def rf_frequency_hz_float(self) -> float:
        return self.rf_frequency_q44_20 / float(1 << Q44_20_SHIFT)

    @property
    def hop_epoch(self) -> int:
        try:
            return int(self.fields["info_source_id"])
        except KeyError as exc:
            raise VrtError("Context carries no info-source (hop epoch) field") from exc


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def _words(data: bytes) -> list[int]:
    if len(data) % 4:
        raise VrtError(f"datagram length {len(data)} is not a whole number of words")
    return list(struct.unpack(f">{len(data) // 4}I", data))


def _prologue(words: list[int]) -> Prologue:
    h = words[0]
    pkt_type = h >> 28
    class_id = bool(h & (1 << 27))
    if not class_id:
        raise VrtError("packets without a Class ID are not produced by the vrt49 framer")
    return Prologue(
        pkt_type=pkt_type,
        class_id_present=class_id,
        trailer_present=bool(h & (1 << 26)),
        tsm=(h >> 24) & 1,
        tsi=(h >> 22) & 3,
        tsf=(h >> 20) & 3,
        pkt_count=(h >> 16) & 0xF,
        pkt_size_words=h & 0xFFFF,
        stream_id=words[1],
        class_oui=words[2] & 0xFFFFFF,
        info_class_code=(words[3] >> 16) & 0xFFFF,
        packet_class_code=words[3] & 0xFFFF,
        int_secs=words[4],
        frac_ps=(words[5] << 32) | words[6],
    )


def parse(data: bytes) -> IfData | Context:
    if len(data) < PROLOGUE_WORDS * 4:
        raise VrtError(f"datagram too short ({len(data)} bytes) for a 7-word prologue")
    words = _words(data)
    p = _prologue(words)
    if p.pkt_size_words != len(words):
        raise VrtError(f"header size {p.pkt_size_words} words != datagram {len(words)} words")
    if p.pkt_type == PKT_IF_DATA_SID:
        return IfData(**vars(p), payload_bytes=data[PROLOGUE_WORDS * 4 :])
    if p.pkt_type == PKT_CONTEXT:
        return _parse_context(p, words)
    raise VrtError(f"unsupported packet type {p.pkt_type}")


def _parse_context(p: Prologue, words: list[int]) -> Context:
    i = PROLOGUE_WORDS
    if i >= len(words):
        raise VrtError("Context packet has no CIF0")
    cif0 = words[i]
    i += 1
    pages = [0]
    cifs = {0: cif0}
    for bit, page in (
        (CIF0_CIF1_ENABLE, 1),
        (CIF0_CIF2_ENABLE, 2),
        (CIF0_CIF3_ENABLE, 3),
        (CIF0_CIF7_ENABLE, 7),
    ):
        if cif0 & (1 << bit):
            if i >= len(words):
                raise VrtError(f"CIF{page} enabled but missing")
            cifs[page] = words[i]
            pages.append(page)
            i += 1
    fields: dict[str, int | tuple[int, ...]] = {}
    for page in pages:
        bitmap = cifs[page]
        for bit in range(31, -1, -1):
            if not bitmap & (1 << bit):
                continue
            if page == 0 and (bit == CIF0_CHANGE or bit in (1, 2, 3, 7)):
                continue
            if page == 0 and bit in _CIF0_VARIABLE:
                raise VrtError(f"variable-size CIF0 field bit {bit} unsupported")
            n = cif_field_words(page, bit)
            if n == 0:
                raise VrtError(f"CIF{page} bit {bit} is a flag or unsupported field")
            if i + n > len(words):
                raise VrtError(f"CIF{page} bit {bit} field truncated")
            block = words[i : i + n]
            i += n
            name = _NAMES.get((page, bit), f"cif{page}_bit{bit}")
            if n <= 2:
                value = 0
                for w in block:
                    value = (value << 32) | w
                fields[name] = value
            else:
                fields[name] = tuple(block)
    if i != len(words):
        raise VrtError(f"Context has {len(words) - i} trailing words")
    return Context(**vars(p), changed=bool(cif0 & (1 << CIF0_CHANGE)), cif=cifs, fields=fields)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def _header(
    pkt_type: int, *, tsm: int, pkt_count: int, size_words: int, tsi: int = 1, tsf: int = 2
) -> int:
    return (
        (pkt_type << 28)
        | (1 << 27)
        | (tsm << 24)
        | (tsi << 22)
        | (tsf << 20)
        | ((pkt_count & 0xF) << 16)
        | (size_words & 0xFFFF)
    )


def _prologue_words(
    pkt_type: int,
    *,
    tsm: int,
    pkt_count: int,
    size_words: int,
    stream_id: int,
    int_secs: int,
    frac_ps: int,
    class_oui: int,
    info_class_code: int,
    packet_class_code: int,
) -> list[int]:
    return [
        _header(pkt_type, tsm=tsm, pkt_count=pkt_count, size_words=size_words),
        stream_id & 0xFFFFFFFF,
        class_oui & 0xFFFFFF,
        ((info_class_code & 0xFFFF) << 16) | (packet_class_code & 0xFFFF),
        int_secs & 0xFFFFFFFF,
        (frac_ps >> 32) & 0xFFFFFFFF,
        frac_ps & 0xFFFFFFFF,
    ]


def build_if_data_words(
    *,
    stream_id: int,
    pkt_count: int,
    int_secs: int,
    frac_ps: int,
    payload_be: bytes,
    class_oui: int = DEFAULT_CLASS_OUI,
    info_class_code: int = DEFAULT_INFO_CLASS,
    packet_class_code: int = DEFAULT_PACKET_CLASS_DATA,
) -> bytes:
    if len(payload_be) % 4:
        raise VrtError("payload must be whole 32-bit words")
    words = _prologue_words(
        PKT_IF_DATA_SID,
        tsm=0,
        pkt_count=pkt_count,
        size_words=PROLOGUE_WORDS + len(payload_be) // 4,
        stream_id=stream_id,
        int_secs=int_secs,
        frac_ps=frac_ps,
        class_oui=class_oui,
        info_class_code=info_class_code,
        packet_class_code=packet_class_code,
    )
    return struct.pack(f">{PROLOGUE_WORDS}I", *words) + payload_be


def build_if_data(
    *, stream_id: int, pkt_count: int, int_secs: int, frac_ps: int, iq: np.ndarray, **kw
) -> bytes:
    arr = np.ascontiguousarray(np.asarray(iq, dtype="<i2").reshape(-1, 2))
    return build_if_data_words(
        stream_id=stream_id,
        pkt_count=pkt_count,
        int_secs=int_secs,
        frac_ps=frac_ps,
        payload_be=arr.tobytes(),
        **kw,
    )


def build_context(
    *,
    stream_id: int,
    pkt_count: int,
    int_secs: int,
    frac_ps: int,
    changed: bool,
    rf_frequency_q44_20: int,
    hop_epoch: int,
    bandwidth_q44_20: int = 0,
    sample_rate_q44_20: int = 0,
    over_range_count: int = 0,
    state_event: int = 0,
    signal_data_format: int = 0,
    discrete_io_32: int = 0,
    class_oui: int = DEFAULT_CLASS_OUI,
    info_class_code: int = DEFAULT_INFO_CLASS,
    packet_class_code: int = DEFAULT_PACKET_CLASS_CONTEXT,
) -> bytes:
    """The 22-word CIF0/1/2 Context the vrt49 framer emits (profile adrv9009-zu11eg-cif012-v1)."""
    cif0 = (
        (1 << 29) | (1 << 27) | (1 << 22) | (1 << 21) | (1 << 16) | (1 << 15) | (1 << 2) | (1 << 1)
    )
    if changed:
        cif0 |= 1 << CIF0_CHANGE
    body = [
        cif0,
        1 << CIF1_DISCRETE_IO_32,
        1 << CIF2_INFO_SOURCE_ID,
        (bandwidth_q44_20 >> 32) & 0xFFFFFFFF,
        bandwidth_q44_20 & 0xFFFFFFFF,
        (rf_frequency_q44_20 >> 32) & 0xFFFFFFFF,
        rf_frequency_q44_20 & 0xFFFFFFFF,
        over_range_count & 0xFFFFFFFF,
        (sample_rate_q44_20 >> 32) & 0xFFFFFFFF,
        sample_rate_q44_20 & 0xFFFFFFFF,
        state_event & 0xFFFFFFFF,
        (signal_data_format >> 32) & 0xFFFFFFFF,
        signal_data_format & 0xFFFFFFFF,
        discrete_io_32 & 0xFFFFFFFF,
        hop_epoch & 0xFFFFFFFF,
    ]
    words = (
        _prologue_words(
            PKT_CONTEXT,
            tsm=1,
            pkt_count=pkt_count,
            size_words=PROLOGUE_WORDS + len(body),
            stream_id=stream_id,
            int_secs=int_secs,
            frac_ps=frac_ps,
            class_oui=class_oui,
            info_class_code=info_class_code,
            packet_class_code=packet_class_code,
        )
        + body
    )
    return struct.pack(f">{len(words)}I", *words)
