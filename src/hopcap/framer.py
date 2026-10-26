# SPDX-License-Identifier: MIT
"""vrt49 framer configuration over its IIO sysfs attributes (the sequence vrt49's SFP
hardware test uses), minus software Context: in hopcap the Context is fabric-owned by the
hop controller, so ``v49_context_enable`` stays 0.

The framer stamps packets from its PPS time base only while ``v49_tb_mode`` is enabled.
vrt49's ZU11EG overlay boots with it off (``adi,timebase-start-disabled``), and with it off
every packet carries the legacy TS registers, which are zero. :func:`configure` therefore arms
the time base, as vrt49's Corundum PPS hardware test does, before it enables the stream."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
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
TIME_BASE_ATTRS = (
    "v49_tb_mode",
    "v49_tb_pps_source",
    "v49_tb_status",
    "v49_tb_seconds",
    "v49_tb_nominal_sps",
    "v49_tb_ps_per_sample_q32",
)
TB_MODE_ENABLE, TB_MODE_STRICT = 0x1, 0x2
LOCK_STATES = ("no_pps", "acquiring", "locked", "holdover")  # TB_STATUS[1:0]
LOCKED = 2


class TimeBaseError(RuntimeError):
    pass


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
    time_base: bool = True
    pps_source: str = "corundum"  # vrt49 overlay adi,pps-source-names: "corundum", "external"
    tb_strict: bool = True  # overlay adi,timebase-strict: hold data while the time base is unlocked
    tb_lock_timeout_s: float = 6.0  # a cold time base locks on its second real PPS edge
    tb_poll_s: float = 0.2

    @property
    def samples_per_packet(self) -> int:
        return (self.mtu_bytes - 28) // 4


def configure(
    fs: Sysfs,
    cfg: FramerConfig,
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Program the endpoint, arm the time base, then enable. Returns the arm record ({} when
    ``cfg.time_base`` is off)."""
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
    record = arm_time_base(fs, cfg, now=now, sleep=sleep) if cfg.time_base else {}
    fs.write("v49_enable", "1")
    return record


def time_base_state(fs: Sysfs) -> dict:
    raw = fs.read_many(list(TIME_BASE_ATTRS))
    status = int(raw["v49_tb_status"].split()[-1], 0)
    return {
        "mode": int(raw["v49_tb_mode"].split()[-1], 0),
        "pps_source": raw["v49_tb_pps_source"].strip(),
        "status": status,
        "lock_state": LOCK_STATES[status & 0x3],
        "missed": (status >> 8) & 0xFF,
        "resync_sticky": bool(status & 0x10),
        "seconds": int(raw["v49_tb_seconds"].split()[-1], 0),
        "nominal_sps": int(raw["v49_tb_nominal_sps"].split()[-1], 0),
        "ps_per_sample_q32": int(raw["v49_tb_ps_per_sample_q32"].split()[-1], 0),
    }


def arm_time_base(
    fs: Sysfs,
    cfg: FramerConfig,
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Arm the next PPS edge with the next host UTC second and enable timestamps. Call with the
    framer disabled. Fails closed unless the time base is LOCKED and its seconds counter has
    taken the armed value: in strict mode an unlocked time base emits no data at all.

    The seconds label is the host clock's (+/-1 s); the sub-second phase is the selected PPS
    (Corundum's free-running PTP PPS by default), so stamps are sample-coherent within and
    across hops but are not GNSS UTC."""
    before = time_base_state(fs)
    fs.write("v49_tb_mode", "0")  # the PPS source is EBUSY while timestamps are enabled
    fs.write("v49_tb_pps_source", cfg.pps_source)
    fs.write("v49_tb_status", "1")  # clear the resync sticky bit so the run's value is its own
    armed = int(now()) + 1
    fs.write("v49_tb_seconds", str(armed))
    fs.write("v49_tb_mode", str(TB_MODE_ENABLE | (TB_MODE_STRICT if cfg.tb_strict else 0)))
    # The arm loads at the next real edge. Wait for the counter to move and require that its
    # first new value is the armed second (or the one after, when it already held that value).
    start = int(fs.read("v49_tb_seconds").split()[-1], 0)
    first_change = None
    status = 0
    seconds = start
    for _ in range(math.ceil(cfg.tb_lock_timeout_s / cfg.tb_poll_s)):
        sleep(cfg.tb_poll_s)
        raw = fs.read_many(["v49_tb_status", "v49_tb_seconds"])
        status = int(raw["v49_tb_status"].split()[-1], 0)
        seconds = int(raw["v49_tb_seconds"].split()[-1], 0)
        if first_change is None and seconds != start:
            first_change = seconds
            if first_change not in (armed, armed + 1):
                raise TimeBaseError(
                    f"armed seconds {armed} did not load: the {cfg.pps_source} PPS edge moved "
                    f"the counter from {start} to {first_change}"
                )
        if first_change is not None and status & 0x3 == LOCKED:
            return {"before": before, "armed_seconds": armed, "after_arm": time_base_state(fs)}
    raise TimeBaseError(
        f"time base not locked {cfg.tb_lock_timeout_s:g} s after arming seconds {armed}: "
        f"lock_state {LOCK_STATES[status & 0x3]}, seconds {seconds}, PPS source "
        f"{cfg.pps_source} (strict mode emits no data until LOCKED)"
    )


def disable(fs: Sysfs) -> None:
    fs.write("v49_enable", "0")


def counters(fs: Sysfs) -> dict[str, int]:
    raw = fs.read_many(list(COUNTERS))
    return {k: int(str(v).split()[-1], 0) for k, v in raw.items()}


def delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: (after[k] - before[k]) & 0xFFFFFFFF for k in before}
