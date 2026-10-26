# SPDX-License-Identifier: MIT
"""Shared fakes: Tick device over tick.hdl_model with a virtual clock, hop-controller MMIO,
Talise stager, framer sysfs with its PPS time base."""

from __future__ import annotations

import errno

from tick import CommandDescriptor, Flag, Status
from tick.hdl_model import AxiTimedCommandSchedulerModel

from hopcap.hopctrl import HopCtrlRegs
from hopcap.plan import FhmPipelineModel

TICK_HZ = 100_000_000


class FakeTickDevice:
    """read_attr/write_attr over the HDL model. Time advances `step` ticks per poll of
    current_time_ticks, so waits terminate without real sleeping."""

    def __init__(self, step: int = 50_000):
        self.model = AxiTimedCommandSchedulerModel(queue_depth=16, tick_hz=TICK_HZ)
        self.now = 0
        self.step = step
        self.pushed: list[CommandDescriptor] = []
        self.max_queue_level = 0

    def _advance(self):
        self.now += self.step
        self.model.advance_to(self.now)

    def read_attr(self, name: str) -> int:
        if name == "current_time_ticks":
            self._advance()
            return self.now
        if name == "time_tick_hz":
            return TICK_HZ
        if name == "queue_depth":
            return 16
        if name == "queue_level":
            return self.model.queue_level
        if name == "capability_bitmap":
            return int(self.model.capability_bitmap)
        if name == "late_count":
            return self.model.late_count
        if name == "overflow_count":
            return self.model.overflow_count
        if name == "completion_sequence_id":
            self._advance()
            c = self.model.completion
            return c.sequence_id if (c and self.model.completion_pending) else 0
        if name == "completion_status":
            return int(Status[self.model.completion.status_name])
        if name == "completion_executed_timestamp_ticks":
            return self.model.completion.executed_timestamp_ticks or 0
        raise KeyError(name)

    def write_attr(self, name: str, value: str) -> None:
        if name == "command_descriptor":
            words = [int(w, 16) for w in value.split(",")]
            payload = b"".join(w.to_bytes(4, "little") for w in words)
            d = CommandDescriptor.unpack(payload)
            self.pushed.append(d)
            self.model.push_gpio(
                d.sequence_id,
                d.timestamp_ticks,
                d.args[0],
                d.args[1],
                late_policy="drop" if d.flags & Flag.LATE_DROP else "execute_now",
                report_completion=bool(d.flags & Flag.REPORT_COMPLETION),
            )
            self.max_queue_level = max(self.max_queue_level, self.model.queue_level)
        elif name == "completion_ack":
            self.model.ack_completion()
        else:
            raise KeyError(name)


class FakeStager:
    def __init__(self, pipeline: FhmPipelineModel):
        self.p = pipeline
        self.staged: list[int] = []
        self.pll = pipeline.init_hz
        self.enabled = False

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False

    def stage(self, hz):
        # Deliberately does NOT touch the pipeline model: the real FhmStager does not either,
        # so TickSession must advance the model itself (review finding #1).
        self.staged.append(hz)

    def readback_hz(self):
        return self.pll

    def park_low(self):
        pass


class FakeMmio:
    """Register-level fake: RW registers store, RO registers are test-settable."""

    def __init__(self, regs: HopCtrlRegs):
        self.r = regs
        self.mem: dict[int, int] = {
            regs.ID: regs.id_word,
            regs.VERSION: regs.version_word,
            regs.SETTLE_TICKS: 0x7D0,
            regs.SEQ_SLOT_COUNT: 8,
        }
        self.writes: list[tuple[int, int]] = []
        self.state = 0
        self.enabled = False
        self.idle_open = True
        self.slot_source = False
        self.superseded_sticky = False
        self.drain_timeout_sticky = False

    def read32(self, off: int) -> int:
        if off == self.r.CTRL_STATUS:
            gate = (
                1
                if (not self.enabled or (self.state == 0 and self.idle_open) or self.state == 4)
                else 0
            )
            return (
                self.state
                | gate << 3
                | self.superseded_sticky << 4
                | self.drain_timeout_sticky << 5
                | self.enabled << 8
                | self.slot_source << 9
                | self.idle_open << 11
            )
        return self.mem.get(off, 0)

    def write32(self, off: int, value: int) -> None:
        self.writes.append((off, value))
        if off == self.r.CTRL_STATUS:
            self.enabled = bool(value & 1)
            self.slot_source = bool(value & 2)
            self.idle_open = bool(value & 8)
            if value & 4:
                self.superseded_sticky = self.drain_timeout_sticky = False
                self.mem[self.r.SUPERSEDED_COUNT] = 0
            return
        self.mem[off] = value


class FakeTimeBase:
    """The vrt49 time base as iio-vrt49-buffer exposes it on the framer device: seconds armed
    through v49_tb_seconds load at the next PPS edge, LOCKED takes two real edges, and
    v49_tb_pps_source is EBUSY while timestamps are enabled. Virtual time advances `step_s` per
    v49_tb_status read, like FakeTickDevice's polls. `pps=False` is a dead PPS input;
    `loads_seconds=False` is a seconds arm that never reaches the sample domain."""

    NAMES = ("corundum", "external")

    def __init__(
        self,
        *,
        pps: bool = True,
        locked: bool = True,
        seconds: int = 0,
        mode: int = 0,
        loads_seconds: bool = True,
        step_s: float = 0.25,
        phase_s: float = 0.6,
    ):
        self.pps, self.loads_seconds, self.step_s = pps, loads_seconds, step_s
        self.t, self.next_edge = 0.0, phase_s
        self.edges = 2 if (pps and locked) else 0
        self.seconds, self.armed = seconds, None
        self.mode, self.source, self.sticky, self.missed = mode, 0, False, 0

    def _advance(self):
        self.t += self.step_s
        while self.pps and self.t >= self.next_edge:
            self.next_edge += 1.0
            self.edges += 1
            if self.armed is not None and self.loads_seconds:
                self.seconds, self.armed = self.armed, None
            else:
                self.seconds = (self.seconds + 1) & 0xFFFFFFFF

    @property
    def lock_state(self) -> int:
        return min(self.edges, 2)

    def read(self, attr: str) -> str:
        if attr == "v49_tb_status":
            self._advance()
            lk = self.lock_state
            return f"0x{(self.missed << 8) | (self.sticky << 4) | ((lk == 2) << 3) | lk:08x}"
        return {
            "v49_tb_mode": f"0x{self.mode:x}",
            "v49_tb_pps_source": self.NAMES[self.source],
            "v49_tb_seconds": str(self.seconds),
            "v49_tb_nominal_sps": "245760000",
            "v49_tb_ps_per_sample_q32": "17476266666667",
        }[attr]

    def write(self, attr: str, value: str) -> None:
        if attr == "v49_tb_mode":
            mode = int(value, 0)
            if mode & ~0x7:
                raise OSError(errno.EINVAL, "v49_tb_mode")
            self.mode = mode
        elif attr == "v49_tb_pps_source":
            if self.mode & 1:
                raise OSError(errno.EBUSY, "v49_tb_pps_source while v49_tb_mode is enabled")
            if value not in self.NAMES:
                raise OSError(errno.EINVAL, "v49_tb_pps_source")
            self.source = self.NAMES.index(value)
        elif attr == "v49_tb_seconds":
            self.armed = int(value, 0)
        elif attr == "v49_tb_status":
            self.sticky = False
        else:
            raise KeyError(attr)


class FakeSysfs:
    """Attribute store with vrt49 framer counter names; writes are recorded in order. The
    v49_tb_* attributes go to a FakeTimeBase (a locked one with live PPS unless given)."""

    def __init__(
        self, counters: dict[str, int] | None = None, time_base: FakeTimeBase | None = None
    ):
        self.attrs: dict[str, str] = {
            "v49_enable": "0",
            "v49_context_enable": "0",
            "v49_packet_mtu": "1468",
            "v49_stream_id": "0",
            "v49_udp_status": "0x1",
            "name": "axi-vrt49-framer",
        }
        for k, v in (counters or {}).items():
            self.attrs[k] = str(v)
        for k in (
            "v49_tx_forwarded_packets",
            "v49_tx_dropped_packets",
            "v49_udp_packet_count",
            "v49_udp_drop_count",
            "v49_udp_length_error_count",
        ):
            self.attrs.setdefault(k, "0")
        self.tb = time_base or FakeTimeBase()
        self.writes: list[tuple[str, str]] = []

    def read(self, attr):
        if attr.startswith("v49_tb_"):
            return self.tb.read(attr)
        return self.attrs[attr]

    def read_int(self, attr):
        return int(self.read(attr).split()[-1], 0)

    def write(self, attr, value):
        if attr.startswith("v49_tb_"):
            self.tb.write(attr, str(value))
        else:
            self.attrs[attr] = str(value)
        self.writes.append((attr, str(value)))

    def read_many(self, attrs):
        return {a: self.read(a) for a in attrs}

    def write_many(self, items):
        for a, v in items:
            self.write(a, v)
