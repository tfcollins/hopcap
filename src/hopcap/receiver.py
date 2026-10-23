# SPDX-License-Identifier: MIT
"""UDP receivers that write the hopcap spool.

:class:`CReceiver` runs ``native/udp_spool.c`` as a subprocess (recvmmsg, 64 MB socket
buffer, every datagram spooled with arrival times) — the only receiver that keeps up with
the ~690 k datagram/s QSFP stream.  :class:`PyReceiver` writes the identical format from a
Python thread and exists for tests and low-rate debugging.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from hopcap import vrt
from hopcap.spool import HEADER_STRUCT

NATIVE_SRC = Path(__file__).resolve().parent / "native" / "udp_spool.c"
READY_RE = re.compile(r"^ready rcvbuf=(\d+)")


class ReceiverError(RuntimeError):
    pass


def compile_udp_spool(out_dir: Path, *, cc: str | None = None) -> Path:
    cc = cc or os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        raise ReceiverError("no C compiler found (set CC or install gcc)")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    binary = out_dir / "udp_spool"
    cmd = [cc, "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(binary), str(NATIVE_SRC)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ReceiverError(f"compiling udp_spool failed:\n{proc.stderr}")
    return binary


def host_rx_counters(interface: str) -> dict:
    """NIC and UDP drop counters that distinguish host loss from FPGA loss (report-only)."""
    out: dict = {"interface": interface}
    stats = Path(f"/sys/class/net/{interface}/statistics")
    for name in (
        "rx_packets",
        "rx_bytes",
        "rx_dropped",
        "rx_missed_errors",
        "rx_errors",
        "rx_fifo_errors",
    ):
        p = stats / name
        if p.exists():
            out[name] = int(p.read_text().strip())
    snmp = Path("/proc/net/snmp")
    if snmp.exists():
        lines = snmp.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.startswith("Udp:") and i + 1 < len(lines):
                keys, vals = line.split()[1:], lines[i + 1].split()[1:]
                out.update({f"udp_{k}": int(v) for k, v in zip(keys, vals, strict=False)})
                break
    softnet = Path("/proc/net/softnet_stat")
    if softnet.exists():
        out["softnet_dropped"] = sum(
            int(line.split()[1], 16) for line in softnet.read_text().splitlines() if line
        )
    return out


class CReceiver:
    def __init__(
        self,
        *,
        bind: tuple[str, int],
        spool_path: Path,
        binary: Path | None = None,
        max_bytes: int | None = None,
        cpu: int | None = None,
        idle_ms: int = 200,
    ):
        self.bind = bind
        self.spool_path = Path(spool_path)
        self.binary = Path(binary) if binary else None
        self.max_bytes = max_bytes
        self.cpu = cpu
        self.idle_ms = idle_ms
        self.proc: subprocess.Popen | None = None
        self.rcvbuf = 0
        self._stderr_lines: list[str] = []
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None

    def _pump_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            self._lines.put(line.rstrip("\n"))
        self._lines.put(None)

    def _wait_line(self, predicate, *, timeout_s: float) -> str | None:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            self._stderr_lines.append(line)
            if predicate(line):
                return line

    def start(self, *, timeout_s: float = 5.0) -> None:
        if self.binary is None:
            self.binary = Path(
                os.environ.get("HOPCAP_UDP_SPOOL")
                or compile_udp_spool(self.spool_path.parent / ".bin")
            )
        cmd = [
            str(self.binary),
            self.bind[0],
            str(self.bind[1]),
            str(self.spool_path),
            "--idle-ms",
            str(self.idle_ms),
        ]
        if self.max_bytes:
            cmd += ["--max-bytes", str(self.max_bytes)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._reader = threading.Thread(target=self._pump_stderr, daemon=True)
        self._reader.start()
        line = self._wait_line(
            lambda ln: READY_RE.match(ln) or "bind" in ln.lower() or "socket" in ln.lower(),
            timeout_s=timeout_s,
        )
        if line is None:
            self.proc.kill()
            self.proc.wait(timeout=1)
            raise ReceiverError(
                f"udp_spool did not report ready (exit {self.proc.returncode}): {self._stderr_lines}"
            )
        m = READY_RE.match(line)
        if not m:
            self.proc.wait(timeout=1)
            raise ReceiverError(f"udp_spool failed to bind {self.bind}: {line.strip()}")
        self.rcvbuf = int(m.group(1))
        if self.cpu is not None and shutil.which("taskset"):
            # Pin after spawn (not via preexec_fn: it aborts under some CPython builds).
            subprocess.run(
                ["taskset", "-pc", str(self.cpu), str(self.proc.pid)], capture_output=True
            )

    def reset(self) -> None:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGUSR1)
        if self._wait_line(lambda ln: ln.startswith("reset"), timeout_s=2.0) is None:
            raise ReceiverError("udp_spool did not acknowledge reset")

    def stop(self, *, timeout_s: float = 10.0) -> dict:
        assert self.proc is not None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            out, _ = self.proc.communicate(
                timeout=timeout_s
            )  # stderr is drained by the reader thread
        except subprocess.TimeoutExpired as exc:
            self.proc.kill()
            self.proc.communicate(timeout=2)
            raise ReceiverError(
                "udp_spool did not exit after SIGTERM (is the framer still streaming?)"
            ) from exc
        if self._reader is not None:
            self._reader.join(timeout=2.0)
        while True:
            try:
                ln = self._lines.get_nowait()
            except queue.Empty:
                break
            if ln is not None:
                self._stderr_lines.append(ln)
        err = "\n".join(self._stderr_lines)
        line = out.strip().splitlines()[-1] if out.strip() else ""
        try:
            summary = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReceiverError(
                f"udp_spool exit {self.proc.returncode}, no summary: {err.strip()}"
            ) from exc
        summary["stderr"] = list(self._stderr_lines)
        summary["exit_code"] = self.proc.returncode
        if self.proc.returncode == 3 or summary.get("stop_reason") == "max-bytes":
            raise ReceiverError(
                f"udp_spool stopped at --max-bytes ({summary.get('spool_bytes_total')} bytes spooled)"
            )
        if self.proc.returncode != 0:
            raise ReceiverError(f"udp_spool exit {self.proc.returncode}: {err.strip()}")
        return summary


class PyReceiver:
    """Reference receiver: same spool bytes as the C helper, Python speed."""

    def __init__(self, *, bind: tuple[str, int], spool_path: Path, rcvbuf: int = 64 << 20):
        self.bind = bind
        self.spool_path = Path(spool_path)
        self.rcvbuf = rcvbuf
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._reset_counters()

    def _reset_counters(self) -> None:
        self.counts = {
            "data_packets": 0,
            "context_packets": 0,
            "malformed": 0,
            "bytes": 0,
            "packet_count_gaps": 0,
            "spool_records": 0,
            "spool_bytes": 0,
        }
        self._last_count: int | None = None

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)
        try:
            self._sock.bind(self.bind)
        except OSError as exc:
            raise ReceiverError(f"bind {self.bind} failed: {exc}") from exc
        self._sock.settimeout(0.05)
        self._fp = self.spool_path.open("wb")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        assert self._sock is not None
        peers_seen: dict[tuple[str, int], int] = {}
        self.peers = peers_seen
        idle_since: float | None = None
        while True:
            try:
                data, (ip, port) = self._sock.recvfrom(9216)
            except TimeoutError:
                if self._stop.is_set():
                    if idle_since is None:
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since >= 0.2:
                        break
                continue
            idle_since = None
            mono, real = time.monotonic_ns(), time.time_ns()
            with self._lock:
                self._fp.write(
                    HEADER_STRUCT.pack(
                        mono, real, len(data), int.from_bytes(socket.inet_aton(ip), "big"), port, 0
                    )
                )
                self._fp.write(data)
                self.counts["spool_records"] += 1
                self.counts["spool_bytes"] += len(data)
                self.counts["bytes"] += len(data)
                peers_seen[(ip, port)] = peers_seen.get((ip, port), 0) + 1
                try:
                    pkt = vrt.parse(data)
                except vrt.VrtError:
                    self.counts["malformed"] += 1
                    continue
                if isinstance(pkt, vrt.Context):
                    self.counts["context_packets"] += 1
                    self._last_count = pkt.pkt_count
                else:
                    self.counts["data_packets"] += 1
                    if (
                        self._last_count is not None
                        and ((self._last_count + 1) & 0xF) != pkt.pkt_count
                    ):
                        self.counts["packet_count_gaps"] += 1
                    self._last_count = pkt.pkt_count
        self._fp.close()

    def reset(self) -> None:
        with self._lock:
            self._reset_counters()

    def stop(self, timeout_s: float = 5.0) -> dict:
        self._stop.set()
        assert self._thread is not None
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            raise ReceiverError("PyReceiver thread did not stop")
        assert self._sock is not None
        self._sock.close()
        return {
            **self.counts,
            "rcvbuf": self.rcvbuf,
            "stop_reason": "stop",
            "peers": [[ip, port, n] for (ip, port), n in self.peers.items()],
            "exit_code": 0,
        }
