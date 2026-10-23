# SPDX-License-Identifier: MIT
"""How the host talks to the board.

* A *shell* runs a command on the target and returns stdout: :class:`SshShell` (plain ssh)
  or anything with the same ``run()`` signature (a labgrid ShellDriver adapter, a fake).
* :class:`HelperMmio` / :class:`HelperSysfs` drive ``target/hopcap_target.py`` with one
  JSON batch per shell call; :class:`DevmemMmio` is the no-python fallback.
* IIO attribute access uses the ``iio_attr`` CLI (works from any host with libiio) or
  pylibiio when available; both reduce to ``(read, write)`` callables.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

HELPER_SRC = Path(__file__).resolve().parent / "target" / "hopcap_target.py"
DEFAULT_HELPER_PATH = "/tmp/hopcap_target.py"
_JSON_DELIM = "HOPCAP_JSON"


class ShellError(RuntimeError):
    pass


class Shell(Protocol):
    def run(self, cmd: str, *, timeout_s: float = 15.0) -> str: ...


class SshShell:
    def __init__(
        self,
        target: str,
        *,
        options: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5"),
    ):
        self.target = target
        self.options = options

    def run(self, cmd: str, *, timeout_s: float = 15.0) -> str:
        proc = subprocess.run(
            ["ssh", *self.options, self.target, cmd],
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
        if proc.returncode != 0:
            raise ShellError(f"ssh {self.target} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout


def upload_file(shell: Shell, local: Path, remote: str) -> None:
    """base64 over the shell: works on a serial console as well as ssh."""
    b64 = base64.b64encode(Path(local).read_bytes()).decode()
    shell.run(
        f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(remote)} && chmod +x {shlex.quote(remote)}"
    )


def _helper_call(
    shell: Shell, helper_path: str, args: str, payload: dict, *, timeout_s: float = 15.0
) -> list:
    body = json.dumps(payload)
    cmd = f"python3 -I {shlex.quote(helper_path)} {args} <<'{_JSON_DELIM}'\n{body}\n{_JSON_DELIM}"
    out = shell.run(cmd, timeout_s=timeout_s)
    line = out.strip().splitlines()[-1] if out.strip() else ""
    try:
        res = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ShellError(f"helper returned no JSON: {out.strip()!r}") from exc
    if "error" in res:
        raise ShellError(res["error"])
    return res["results"]


@dataclass
class _Pending:
    index: int
    value: int | None = None


class _MmioBatch:
    def __init__(self):
        self.ops: list[dict] = []
        self.pending: list[_Pending] = []

    def read32(self, off: int) -> _Pending:
        p = _Pending(len(self.ops))
        self.ops.append({"op": "r", "off": off})
        self.pending.append(p)
        return p

    def write32(self, off: int, val: int) -> None:
        self.ops.append({"op": "w", "off": off, "val": val & 0xFFFFFFFF})


class HelperMmio:
    def __init__(
        self, shell: Shell, *, base: int, helper_path: str = DEFAULT_HELPER_PATH, span: int = 0x1000
    ):
        self.shell, self.base, self.helper_path, self.span = shell, base, helper_path, span

    def ensure_uploaded(self) -> None:
        upload_file(self.shell, HELPER_SRC, self.helper_path)

    @contextmanager
    def batch(self):
        b = _MmioBatch()
        yield b
        if not b.ops:
            return
        results = _helper_call(
            self.shell,
            self.helper_path,
            f"mmio --base 0x{self.base:08x} --span 0x{self.span:x}",
            {"ops": b.ops},
        )
        for p in b.pending:
            p.value = results[p.index]

    def read32(self, off: int) -> int:
        with self.batch() as b:
            p = b.read32(off)
        return int(p.value)

    def write32(self, off: int, val: int) -> None:
        with self.batch() as b:
            b.write32(off, val)


class DevmemMmio:
    """Fallback for boards without python3: one ``devmem`` per access."""

    def __init__(self, shell: Shell, *, base: int):
        self.shell, self.base = shell, base

    def read32(self, off: int) -> int:
        out = self.shell.run(f"devmem 0x{self.base + off:08X} 32")
        return int(out.strip().split()[-1], 0)

    def write32(self, off: int, val: int) -> None:
        self.shell.run(f"devmem 0x{self.base + off:08X} 32 0x{val & 0xFFFFFFFF:08X}")


class HelperSysfs:
    def __init__(self, shell: Shell, *, device_dir: str, helper_path: str = DEFAULT_HELPER_PATH):
        self.shell, self.device_dir, self.helper_path = shell, device_dir, helper_path

    def read(self, attr: str) -> str:
        return str(
            _helper_call(
                self.shell,
                self.helper_path,
                f"sysfs --dir {shlex.quote(self.device_dir)}",
                {"ops": [{"op": "r", "attr": attr}]},
            )[0]
        )

    def read_int(self, attr: str) -> int:
        return int(self.read(attr).split()[-1], 0)

    def write(self, attr: str, value: str) -> None:
        _helper_call(
            self.shell,
            self.helper_path,
            f"sysfs --dir {shlex.quote(self.device_dir)}",
            {"ops": [{"op": "w", "attr": attr, "val": str(value)}]},
        )

    def read_many(self, attrs: list[str]) -> dict[str, str]:
        res = _helper_call(
            self.shell,
            self.helper_path,
            f"sysfs --dir {shlex.quote(self.device_dir)}",
            {"ops": [{"op": "r", "attr": a} for a in attrs]},
        )
        return dict(zip(attrs, (str(v) for v in res), strict=True))

    def write_many(self, items: list[tuple[str, str]]) -> None:
        _helper_call(
            self.shell,
            self.helper_path,
            f"sysfs --dir {shlex.quote(self.device_dir)}",
            {"ops": [{"op": "w", "attr": a, "val": str(v)} for a, v in items]},
        )


def find_iio_device_dir(shell: Shell, name: str, *, helper_path: str = DEFAULT_HELPER_PATH) -> str:
    out = shell.run(f"python3 -I {shlex.quote(helper_path)} find --name {shlex.quote(name)}")
    res = json.loads(out.strip().splitlines()[-1])
    if "error" in res:
        raise ShellError(res["error"])
    return res["path"]


# ---------------------------------------------------------------------------
# IIO attribute access
# ---------------------------------------------------------------------------

_VALUE_RE = re.compile(r"value '([^']*)'")


def parse_iio_attr_value(text: str) -> str:
    m = _VALUE_RE.search(text)
    return m.group(1) if m else text.strip()


def iio_channel_cli_io(
    uri: str,
    device: str,
    channel: str,
    *,
    output: bool = True,
    runner: Callable[[list[str]], str] | None = None,
    timeout_s: float = 10.0,
) -> tuple[Callable[[str], str], Callable[[str, str], None]]:
    def default_runner(args: list[str]) -> str:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout_s, check=True)
        return proc.stdout

    run = runner or default_runner
    base = ["iio_attr", "-u", uri, "-c"] + (["-o"] if output else []) + [device, channel]

    def read_attr(name: str) -> str:
        return parse_iio_attr_value(run([*base, name]))

    def write_attr(name: str, value: str) -> None:
        run([*base, name, value])

    return read_attr, write_attr


def iio_device_cli_io(
    uri: str,
    device: str,
    *,
    runner: Callable[[list[str]], str] | None = None,
    timeout_s: float = 10.0,
) -> tuple[Callable[[str], int], Callable[[str, str], None]]:
    """Device-attribute adapter with Tick's int-parsing convention (last token, base 0)."""

    def default_runner(args: list[str]) -> str:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout_s, check=True)
        return proc.stdout

    run = runner or default_runner

    def read_attr(name: str) -> int:
        return int(
            parse_iio_attr_value(run(["iio_attr", "-u", uri, "-d", device, name])).split()[-1], 0
        )

    def write_attr(name: str, value: str) -> None:
        run(["iio_attr", "-u", uri, "-d", device, name, value])

    return read_attr, write_attr


def find_fhm_channel_cli(uri: str, device: str = "adrv9009-phy", *, runner=None) -> str:
    """Name of the LO channel exposing frequency_hopping_mode_enable (demo: Talise A TRX_LO)."""

    def default_runner(args):
        return subprocess.run(args, text=True, capture_output=True, timeout=10, check=True).stdout

    run = runner or default_runner
    listing = run(["iio_attr", "-u", uri, "-c", "-o", device])
    for m in re.finditer(r"channel '([^']+)'", listing):
        chan = m.group(1)
        attrs = run(["iio_attr", "-u", uri, "-c", "-o", device, chan])
        if "frequency_hopping_mode_enable" in attrs:
            return chan
    raise ShellError(f"no output channel on {device} exposes frequency_hopping_mode_enable")
