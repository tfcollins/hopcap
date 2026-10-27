# SPDX-License-Identifier: MIT
from __future__ import annotations

import json

import pytest

from hopcap.transport import (
    DevmemMmio,
    HelperMmio,
    HelperSysfs,
    ShellError,
    iio_channel_cli_io,
    parse_iio_attr_value,
)


class FakeShell:
    def __init__(self, responder):
        self.cmds: list[str] = []
        self.responder = responder

    def run(self, cmd: str, *, timeout_s: float = 15.0) -> str:
        self.cmds.append(cmd)
        return self.responder(cmd)


def test_helper_mmio_batches_reads_and_writes_in_one_shell_call():
    def respond(cmd):
        assert "hopcap_target.py mmio --base 0x84ac0000" in cmd
        payload = json.loads(cmd.split("<<'HOPCAP_JSON'\n", 1)[1].split("\nHOPCAP_JSON", 1)[0])
        results = [None if op["op"] == "w" else 0x484F5032 for op in payload["ops"]]
        return json.dumps({"results": results})

    sh = FakeShell(respond)
    m = HelperMmio(sh, base=0x84AC0000, helper_path="/tmp/hopcap_target.py")
    with m.batch() as b:
        b.write32(0x0C, 5)
        rd = b.read32(0x00)
    assert rd.value == 0x484F5032
    assert len(sh.cmds) == 1
    # single-op convenience path still works
    assert m.read32(0x00) == 0x484F5032
    assert len(sh.cmds) == 2


def test_devmem_fallback_formats_commands():
    def respond(cmd):
        if cmd.startswith("devmem 0x84AC0000 32"):
            return "0x484F5032\n"
        return ""

    sh = FakeShell(respond)
    m = DevmemMmio(sh, base=0x84AC0000)
    assert m.read32(0) == 0x484F5032
    m.write32(0x0C, 0x7D0)
    assert sh.cmds[-1] == "devmem 0x84AC000C 32 0x000007D0"


def test_helper_sysfs_read_write():
    def respond(cmd):
        payload = json.loads(cmd.split("<<'HOPCAP_JSON'\n", 1)[1].split("\nHOPCAP_JSON", 1)[0])
        return json.dumps({"results": [None if op["op"] == "w" else "42" for op in payload["ops"]]})

    sh = FakeShell(respond)
    fs = HelperSysfs(
        sh, device_dir="/sys/bus/iio/devices/iio:device3", helper_path="/tmp/hopcap_target.py"
    )
    fs.write("v49_enable", "1")
    assert fs.read("v49_tx_dropped_packets") == "42"
    assert fs.read_int("v49_tx_dropped_packets") == 42


def test_helper_error_surfaces_as_shell_error():
    sh = FakeShell(lambda cmd: json.dumps({"error": "mmio offset 0x1002 not 4-byte aligned"}))
    m = HelperMmio(sh, base=0, helper_path="/tmp/h.py")
    with pytest.raises(ShellError, match="aligned"):
        m.read32(0x1002)


def test_parse_iio_attr_value():
    line = "dev 'adrv9009-phy', channel 'altvoltage0' (output), attr 'frequency_hopping_mode', value '2450000000'"
    assert parse_iio_attr_value(line) == "2450000000"
    assert parse_iio_attr_value("2450000000\n") == "2450000000"


def test_iio_channel_cli_io_builds_iio_attr_commands():
    calls = []

    def runner(args):
        calls.append(args)
        return "dev 'adrv9009-phy', channel 'altvoltage0' (output), attr 'frequency_hopping_mode', value '2410000000'\n"

    read_attr, write_attr = iio_channel_cli_io(
        "ip:10.0.0.79", "adrv9009-phy", "altvoltage0", output=True, runner=runner
    )
    assert read_attr("frequency_hopping_mode") == "2410000000"
    write_attr("frequency_hopping_mode", "2420000000")
    assert calls[0][:1] == ["iio_attr"] and "-u" in calls[0] and "-o" in calls[0]
    assert calls[1][-2:] == ["frequency_hopping_mode", "2420000000"]
