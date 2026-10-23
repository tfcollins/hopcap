# SPDX-License-Identifier: MIT
"""vrt49 framer configuration over its IIO sysfs attributes (the sequence vrt49's SFP
hardware test uses), minus software Context: in hopcap the Context is fabric-owned by the
hop controller, so ``v49_context_enable`` stays 0."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

COUNTERS = (
    "v49_tx_forwarded_packets",
    "v49_tx_dropped_packets",
    "v49_udp_packet_count",
    "v49_udp_drop_count",
    "v49_udp_length_error_count",
)
HARD_FAIL_COUNTERS = ("v49_tx_dropped_packets", "v49_udp_drop_count", "v49_udp_length_error_count")


class Sysfs(Protocol):
    def read(self, attr: str) -> str: ...
    def read_int(self, attr: str) -> int: ...
    def write(self, attr: str, value: str) -> None: ...
    def read_many(self, attrs: list[str]) -> dict[str, str]: ...
    def write_many(self, items: list[tuple[str, str]]) -> None: ...


@dataclass(frozen=True)
class FramerConfig:
    stream_id: int = 0x01000000
    mtu_bytes: int = 1468  # 367 words: 7 prologue + 360 CI16 samples
    dest_ip: str = "192.0.2.2"
    dest_port: int = 4991
    source_ip: str = "192.0.2.1"
    source_port: int = 4991
    dest_mac: str = "64:9d:99:6f:a9:6a"  # nemo XL710 enp4s0f0np0 (verify in HOSTS.md)
    source_mac: str = "02:00:00:00:00:01"
    ttl: int = 64

    @property
    def samples_per_packet(self) -> int:
        return (self.mtu_bytes - 28) // 4


def configure(fs: Sysfs, cfg: FramerConfig) -> None:
    fs.write("v49_enable", "0")
    fs.write("v49_context_enable", "0")
    fs.write_many(
        [
            ("v49_packet_mtu", str(cfg.mtu_bytes)),
            ("v49_stream_id", f"0x{cfg.stream_id:08x}"),
            ("v49_udp_dest_ip", cfg.dest_ip),
            ("v49_udp_source_ip", cfg.source_ip),
            ("v49_udp_dest_port", str(cfg.dest_port)),
            ("v49_udp_source_port", str(cfg.source_port)),
            ("v49_udp_ttl", str(cfg.ttl)),
            ("v49_udp_dest_mac", cfg.dest_mac),
            ("v49_udp_source_mac", cfg.source_mac),
        ]
    )
    fs.write("v49_udp_apply", "1")
    fs.write("v49_enable", "1")


def disable(fs: Sysfs) -> None:
    fs.write("v49_enable", "0")


def counters(fs: Sysfs) -> dict[str, int]:
    raw = fs.read_many(list(COUNTERS))
    return {k: int(str(v).split()[-1], 0) for k, v in raw.items()}


def delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: (after[k] - before[k]) & 0xFFFFFFFF for k in before}
