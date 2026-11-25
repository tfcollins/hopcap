# SPDX-License-Identifier: MIT
"""The lablp board session against a fake board: stage order, the runtime-load script's own
order (AD9517 before the PL load), RX channels, UDP endpoint, PCS and time-base gates."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes import FakeSysfs, FakeTimeBase
from test_timebase import SimBoard

from hopcap.lab import lablp
from hopcap.lab.lablp import Artifacts, LablpSession, SessionError

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/lablp_hopcap_runtime_load.sh").read_text()
FS = 15_360_000
RELEASE = "6.1.70-284114-g2e8908932dfd"


class FakeBoard(SimBoard):
    """Kuiper on lablp, as the session's shell sees it."""

    target = "root@10.9.9.9"

    def __init__(self, *, release=RELEASE, load_ok=True, scan_ok=True, fs_readback=FS):
        super().__init__()
        self.release, self.load_ok, self.scan_ok, self.fs_readback = (
            release,
            load_ok,
            scan_ok,
            fs_readback,
        )
        self.pushed: dict[str, str] = {}
        self.order: list[str] = []

    def push(self, local, remote, *, timeout_s=300.0):
        self.order.append(f"push {Path(remote).name}")
        digest = hashlib.sha256(Path(local).read_bytes()).hexdigest()
        self.pushed[Path(remote).name] = digest
        return digest

    def run(self, cmd, *, timeout_s=15.0):
        if cmd == "uname -r":
            return self.release + "\n"
        if "lablp_hopcap_runtime_load.sh" in cmd:
            self.order.append("load")
            assert cmd.split()[2:6] == [
                "/tmp/hopcap/system_top.bin",
                "/tmp/hopcap/hopcap.dtbo",
                "/tmp/hopcap/axi_timed_command_scheduler.ko",
                "/tmp/hopcap/iio-vrt49-buffer.ko",
            ]
            lines = [
                "ad9517_out4_frequency=125000000",
                "ad9517_out4_raw=1",
                "fpga_manager_state=operating",
                f"bin_sha256={self.pushed['system_top.bin']}",
            ]
            if self.load_ok:
                lines.append("hopcap_runtime_load=ok")
            return "\n".join(lines) + "\n"
        if "in_voltage_sampling_frequency" in cmd or "TX1_I_F1" in cmd:
            self.order.append("radio")
            attrs = re.findall(r'echo "ATTR (\S+) ', cmd)
            vals = re.findall(r"echo (\S+) > \"\$D/\"", cmd)
            out = []
            for a, v in zip(attrs, vals, strict=True):
                if a == "in_voltage_sampling_frequency":
                    v = str(self.fs_readback)
                out.append(f"ATTR {a} {v.strip(chr(39))}")
            return "\n".join(out) + "\n"
        if "scan_elements" in cmd:
            self.order.append("rx_buffer")
            on = (
                ("voltage0", "voltage1", "voltage2", "voltage3")
                if self.scan_ok
                else ("voltage0", "voltage1")
            )
            scans = [
                f"SCAN in_{c} {int(c in on)}"
                for c in ("voltage0", "voltage1", "voltage2", "voltage3")
            ]
            return "BUFFER 1 65536\n" + "\n".join(scans) + "\nSCAN in_timestamp 0\n"
        if "ARMED" in cmd or "CHECK" in cmd:
            self.order.append("align" if "ARMED" in cmd else "align-check")
            return super().run(cmd, timeout_s=timeout_s)
        raise AssertionError(cmd)


class FramerMmio:
    def __init__(self, pcs=0x0001D80B, period=FS + 300):
        self.regs = {0x78: pcs, 0x60: period, 0x64: FS, 0x68: 15_360}

    def read32(self, off):
        return self.regs[off]


def artifacts(tmp_path) -> Artifacts:
    for name in ("system_top.bin", "tick.ko", "vrt49.ko", "o.dtbo"):
        (tmp_path / name).write_bytes(name.encode() * 10)
    return Artifacts(
        tmp_path / "system_top.bin",
        tmp_path / "tick.ko",
        tmp_path / "vrt49.ko",
        overlay_dtbo=tmp_path / "o.dtbo",
    )


def host_cmd(argv, **kw):
    assert argv[:3] == ["modinfo", "-F", "vermagic"]
    return subprocess.CompletedProcess(argv, 0, stdout=RELEASE + " SMP preempt mod_unload ARMv7\n")


def session(tmp_path, board=None, mmio=None):
    board = board or FakeBoard()
    fs = FakeSysfs(
        time_base=FakeTimeBase(
            names=("tick", "disabled"), nominal_sps=FS, ps_per_sample_q32=279620266666667
        )
    )
    fs.attrs.update({"v49_udp_dest_ip": "", "v49_udp_dest_mac": "", "v49_udp_source_ip": ""})
    s = LablpSession(
        shell=board,
        iio_uri="ip:10.9.9.9",
        artifacts=artifacts(tmp_path),
        evidence=tmp_path / "ev",
        iio_list=lambda: "\n".join(lablp.REQUIRED_DEVICES),
        iio_read=lambda dev, attr: "50000000",
        run_cmd=host_cmd,
        sleep=lambda s: None,
        monotonic=fs.tb.clock,
        log=lambda m: None,
        framer_io=lambda: (fs, mmio or FramerMmio()),
    )
    return s, board, fs


def test_bring_up_runs_every_stage_in_order(tmp_path):
    s, board, fs = session(tmp_path)
    rec = s.bring_up()
    assert list(rec) == [
        "connect",
        "push",
        "load",
        "devices",
        "radio",
        "rx_buffer",
        "link",
        "timebase",
    ]
    assert board.order[:5] == [
        f"push {n}"
        for n in (
            "system_top.bin",
            "hopcap.dtbo",
            "axi_timed_command_scheduler.ko",
            "iio-vrt49-buffer.ko",
            "lablp_hopcap_runtime_load.sh",
        )
    ]
    assert board.order[5:] == ["load", "radio", "radio", "rx_buffer", "align", "align-check"]
    assert rec["connect"]["kernel_release"] == RELEASE
    assert rec["devices"]["time_tick_hz"] == 50_000_000
    assert rec["rx_buffer"]["scan"]["in_voltage3"] is True
    # UDP destination = nemo enp7s0, applied, then PCS resetdone checked
    assert fs.attrs["v49_udp_dest_ip"] == "192.168.2.10"
    assert fs.attrs["v49_udp_dest_mac"] == "68:05:ca:19:88:8c"
    assert ("v49_udp_apply", "1") in fs.writes and fs.attrs["v49_enable"] == "0"
    assert rec["link"]["pcs"]["resetdone"] is True
    tb = rec["timebase"]
    assert tb["armed"]["after_arm"]["pps_source"] == "tick"
    assert tb["pps_period"]["beats_per_pps"] == FS + 300
    assert tb["tick_alignment"]["time_aligned"] == 1
    saved = json.loads((tmp_path / "ev/session.json").read_text())
    assert saved["load"]["hopcap_runtime_load"] == "ok"
    radio = rec["radio"]["readback"]
    assert radio["cf-ad9361-dds-core-lpc"]["out_altvoltage0_TX1_I_F1_frequency"] == "1000000"


@pytest.mark.parametrize(
    "board, mmio, match",
    [
        (dict(release="6.1.70-other"), None, "stock Kuiper"),
        (dict(load_ok=False), None, "runtime load did not finish"),
        (dict(fs_readback=30_720_000), None, "sample rate"),
        (dict(scan_ok=False), None, "in_voltage2"),
        ({}, dict(pcs=0x0000D80B), "resetdone"),
        ({}, dict(period=FS // 2), "four RX"),
    ],
)
def test_bring_up_fails_closed(tmp_path, board, mmio, match):
    s, _b, _fs = session(tmp_path, FakeBoard(**board), FramerMmio(**mmio) if mmio else None)
    with pytest.raises((SessionError, RuntimeError), match=match):
        s.bring_up()
    saved = json.loads((tmp_path / "ev/session.json").read_text())
    assert any("error" in v for v in saved.values() if isinstance(v, dict))


def test_vermagic_mismatch_is_refused(tmp_path):
    s, _b, _fs = session(tmp_path)
    s.run_cmd = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="6.1.70-x SMP\n")
    with pytest.raises(SessionError, match="vermagic"):
        s.bring_up()


def test_target_from_uri():
    assert lablp.target_from_uri("ip:10.0.3.126") == "10.0.3.126"
    with pytest.raises(SessionError):
        lablp.target_from_uri("local:")


def test_runtime_load_script_order():
    """AD9517 OUT4 before any unbind and before the FPGA manager; unbind, load, rebind,
    overlay, modules, iiod; the modules' dependencies resolved before insmod."""
    idx = {
        k: SCRIPT.index(k)
        for k in (
            'echo 125000000 > "$clk/out_altvoltage4_frequency"',
            'echo 1 > "$clk/out_altvoltage4_raw"',
            'unbind "$DDS" "$DDS_DEV"',
            'echo hopcap-z7035.bin > "$mgr/firmware"',
            '\nbind "$DDS" "$DDS_DEV"',
            'cat "$dtbo" > "$OVL/dtbo"',
            'insmod "$ko"',
            "systemctl start iiod",
        )
    }
    assert list(idx.values()) == sorted(idx.values())
    assert SCRIPT.index('modprobe "$dep"') < SCRIPT.index('insmod "$ko"')
    for dev in (
        "79024000.cf-ad9361-dds-core-lpc",
        "79020000.cf-ad9361-lpc",
        "spi0.0",
        "7c400000.dma 7c420000.dma",
    ):
        assert dev in SCRIPT
    assert (
        subprocess.run(["sh", "-n", str(ROOT / "scripts/lablp_hopcap_runtime_load.sh")]).returncode
        == 0
    )


def test_board_snippets_are_valid_sh():
    snippets = [
        lablp.sysfs_write_snippet(dev, items)
        for dev, items in lablp.radio_items(lablp.RadioConfig()).items()
    ] + [lablp.rx_buffer_snippet("cf-ad9361-lpc"), lablp.rx_buffer_off_snippet("cf-ad9361-lpc")]
    for cmd in snippets:
        assert subprocess.run(["sh", "-n", "-c", cmd]).returncode == 0, cmd


def test_rx_buffer_snippet_runs_against_a_fake_sysfs(tmp_path):
    """Execute the real snippet with /sys/bus/iio/devices pointed at a temp tree."""
    dev = tmp_path / "sys/bus/iio/devices/iio:device3"
    (dev / "scan_elements").mkdir(parents=True)
    (dev / "buffer").mkdir()
    (dev / "name").write_text("cf-ad9361-lpc\n")
    for c in ("voltage0", "voltage1", "voltage2", "voltage3", "timestamp"):
        (dev / f"scan_elements/in_{c}_en").write_text("1\n" if c == "timestamp" else "0\n")
    (dev / "buffer/enable").write_text("0\n")
    (dev / "buffer/length").write_text("0\n")
    cmd = lablp.rx_buffer_snippet("cf-ad9361-lpc").replace(
        "/sys/bus/iio", f"{tmp_path}/sys/bus/iio"
    )
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=True).stdout
    got = lablp.parse_buffer(out)
    assert got["enabled"] and got["length"] == 65536
    assert got["scan"] == {
        "in_timestamp": False,
        "in_voltage0": True,
        "in_voltage1": True,
        "in_voltage2": True,
        "in_voltage3": True,
    }


def test_module_build_script_dry_run(tmp_path):
    linux = tmp_path / "linux"
    (linux / ".git").mkdir(parents=True)
    (linux / "Makefile").write_text("VERSION = 6\n")
    r = subprocess.run(
        [
            str(ROOT / "scripts/build_lablp_modules.sh"),
            "--linux-dir",
            str(linux),
            "--work-dir",
            str(tmp_path / "w"),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert re.search(r"--localversion=\\?'-284114", r.stdout)
    assert re.search(r"--expect-kernelrelease=\\?'6\.1\.70-284114-g2e8908932dfd", r.stdout)
    assert "ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf-" in r.stdout
    assert "kuiper-2023_r2_p1-zynq-adrv9361-z7035.config.gz" in r.stdout
    assert "M=/work/src/$m" in r.stdout and "tick-kernel iio-vrt49-buffer" in r.stdout
    assert "/repo" not in r.stdout, "never mount the worktree / submodules"
    assert not (tmp_path / "w").exists(), "dry run has no side effects"
    # a worktree (".git" is a file) is refused
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "Makefile").write_text("x")
    (wt / ".git").write_text("gitdir: /elsewhere")
    r = subprocess.run(
        [
            str(ROOT / "scripts/build_lablp_modules.sh"),
            "--linux-dir",
            str(wt),
            "--work-dir",
            str(tmp_path / "w"),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2 and "worktree" in r.stderr
