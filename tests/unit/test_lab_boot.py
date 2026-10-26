# SPDX-License-Identifier: MIT
"""lab/boot.py: the pure parts of vrt49's corundum_shell SD/JTAG boot (commands, env contract)."""

from __future__ import annotations

import pytest

from hopcap.lab import boot

SHA = "a" * 64


def test_boot_artifacts_from_env_requires_complete_sd_set():
    env = {
        "LG_ENV": "/tmp/tron.yaml",
        "VRT49_CORUNDUM_BOOT": "sd",
        "VRT49_CORUNDUM_BOOT_BIN_URL": "http://x/BOOT.BIN",
        "VRT49_CORUNDUM_BOOT_BIN_SHA256": SHA,
        "VRT49_CORUNDUM_KERNEL_URL": "http://x/Image",
        "VRT49_CORUNDUM_KERNEL_SHA256": SHA,
        "VRT49_CORUNDUM_DTB_URL": "http://x/system.dtb",
        "VRT49_CORUNDUM_DTB_SHA256": SHA,
    }
    a = boot.BootArtifacts.from_env(env)
    assert a.mode == "sd" and a.env_path == "/tmp/tron.yaml"
    assert a.staged_names() == {
        "BOOT.BIN": ("http://x/BOOT.BIN", SHA),
        "Image": ("http://x/Image", SHA),
        "system.dtb": ("http://x/system.dtb", SHA),
    }
    del env["VRT49_CORUNDUM_DTB_SHA256"]
    with pytest.raises(boot.BootConfigError, match="together"):
        boot.BootArtifacts.from_env(env)


def test_jtag_mode_needs_bitstream_and_psu_init_and_stages_beside_stock():
    env = {
        "LG_ENV": "/tmp/tron.yaml",
        "VRT49_CORUNDUM_BOOT": "jtag",
        "VRT49_CORUNDUM_KERNEL_URL": "http://x/Image",
        "VRT49_CORUNDUM_KERNEL_SHA256": SHA,
        "VRT49_CORUNDUM_DTB_URL": "http://x/system.dtb",
        "VRT49_CORUNDUM_DTB_SHA256": SHA,
    }
    with pytest.raises(boot.BootConfigError, match="BITSTREAM"):
        boot.BootArtifacts.from_env(env)
    env["VRT49_CORUNDUM_BITSTREAM_REMOTE"] = "/srv/system_top.bit"
    env["VRT49_CORUNDUM_PSU_INIT_REMOTE"] = "/srv/psu_init.tcl"
    a = boot.BootArtifacts.from_env(env)
    assert set(a.staged_names()) == {"Image.vrt49.test", "system.vrt49.dtb"}


def test_stage_command_downloads_beside_then_verifies_then_renames():
    cmd = boot.stage_command("http://x/Image", SHA, "Image")
    assert "wget -q -O /boot/Image.new" in cmd
    assert f"{SHA}  /boot/Image.new" in cmd and "sha256sum -c -" in cmd
    assert cmd.index("sha256sum") < cmd.index("mv /boot/Image.new /boot/Image")


def test_sd_backup_is_idempotent_and_restore_removes_backup():
    b = boot.sd_backup_command()
    assert (
        "if [ ! -d /boot/vrt49-stock ]" in b
        and "cp /boot/BOOT.BIN /boot/Image /boot/system.dtb" in b
    )
    r = boot.sd_restore_command()
    assert "cp /boot/vrt49-stock/BOOT.BIN" in r and "rm -rf /boot/vrt49-stock" in r


def test_sd_boot_command_imports_uenv_without_running_uenvcmd():
    c = boot.CORUNDUM_SD_BOOT_COMMAND
    assert "setenv uenvcmd;" in c and "booti $kernel_addr - $fdt_addr" in c
    assert "system.vrt49.dtb" in c and "Image.vrt49.test" in c


def test_shell_value_strips_prompt_noise():
    assert boot.parse_shell_value(["", "abc  def", ""]) == "abc  def"


# --- power state across a failed SD boot ---------------------------------------------------


def _status_enum():
    """The plugin's Status enum when adi_lg_plugins is installed, else a stand-in module."""
    import enum
    import sys
    import types

    try:
        from adi_lg_plugins.strategies.bootzynqmpjtag import Status
    except ImportError:
        Status = enum.Enum("Status", "unknown powered_off powered_on kuiper_shell", start=0)
        mod = types.ModuleType("adi_lg_plugins.strategies.bootzynqmpjtag")
        mod.Status = Status
        for name in ("adi_lg_plugins", "adi_lg_plugins.strategies"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules[mod.__name__] = mod
    return Status


class _Outlet:
    def __init__(self):
        self.on_ = None

    def on(self):
        self.on_ = True

    def off(self):
        self.on_ = False


class _DeadConsole:
    def expect(self, pattern, timeout=None):
        raise OSError("read failed: socket disconnected")


class _Strategy:
    """BootZynqMPJTAG's transition contract for powered_off: a no-op when its recorded status is
    already powered_off, otherwise outlet off."""

    kuiper_kernel_marker, kuiper_shell_marker, kuiper_boot_timeout = "Starting kernel", "#", 1

    def __init__(self, status_enum):
        self.S = status_enum
        self.status = status_enum.unknown
        self.power = _Outlet()
        self.shell = type("Shell", (), {"console": _DeadConsole()})()

    def transition(self, status):
        status = self.S[status] if isinstance(status, str) else status
        if status == self.status:
            return
        if status == self.S.powered_off:
            self.power.off()
        self.status = status

    def _activate_shell_bypass(self):
        return self.shell


class _Target:
    def activate(self, _driver):
        pass

    def cleanup(self):
        pass


def test_a_failed_sd_boot_still_powers_the_board_off(tmp_path):
    """tron 2026-10-25: the console dropped 2.7 s after power-on, and the board was left on
    because the strategy still believed it was powered_off."""
    strategy = _Strategy(_status_enum())
    artifacts = boot.BootArtifacts(
        env_path=str(tmp_path / "env.yaml"),
        mode="sd",
        kernel=("http://x/Image", SHA),
        dtb=("http://x/system.dtb", SHA),
        boot_bin=("http://x/BOOT.BIN", SHA),
        bitstream_remote=None,
        psu_init_remote=None,
    )
    session = boot.TronSession(artifacts, log=lambda _m: None)
    session._new_target = lambda **_kw: (_Target(), strategy)
    with pytest.raises(OSError, match="socket disconnected"):
        session.boot_exact()
    assert strategy.power.on_ is True
    session.restore_and_power_off()
    assert strategy.power.on_ is False, "board left powered after a failed boot"


def test_staged_sums_command_hashes_exactly_the_staged_names():
    jtag = boot.BootArtifacts(
        env_path="/x",
        mode="jtag",
        kernel=("http://x/Image", SHA),
        dtb=("http://x/system.dtb", SHA),
        boot_bin=None,
        bitstream_remote="/tmp/j/system_top.bit",
        psu_init_remote="/tmp/j/psu_init.tcl",
    )
    assert (
        boot.staged_sums_command(jtag.staged_names())
        == "sha256sum /boot/Image.vrt49.test /boot/system.vrt49.dtb"
    )


JTAG_PL = {
    "LG_ENV": "/tmp/tron.yaml",
    "VRT49_CORUNDUM_BITSTREAM_REMOTE": "/tmp/j/system_top.bit",
    "VRT49_CORUNDUM_PSU_INIT_REMOTE": "/tmp/j/psu_init.tcl",
}


def test_boot_mode_defaults_to_the_place_tag():
    seen = []

    def resolve():
        seen.append(1)
        return {"mode": "jtag", "boot_strategy": "BootZynqMPJTAG"}

    a = boot.BootArtifacts.from_env(JTAG_PL, resolve_mode=resolve)
    assert seen and a.mode == "jtag"
    assert a.mode_source == "place tron boot-strategy=BootZynqMPJTAG"
    a = boot.BootArtifacts.from_env(
        {"LG_ENV": "/x", "VRT49_CORUNDUM_BOOT": "auto"},
        resolve_mode=lambda: {"mode": "sd", "boot_strategy": "BootFPGASoC"},
    )
    assert a.mode == "sd" and a.mode_source == "place tron boot-strategy=BootFPGASoC"
    env = dict(JTAG_PL)
    del env["VRT49_CORUNDUM_PSU_INIT_REMOTE"]
    with pytest.raises(boot.BootConfigError, match="PSU_INIT"):
        boot.BootArtifacts.from_env(env, resolve_mode=resolve)


def test_explicit_boot_mode_never_queries_the_place():
    def resolve():
        raise AssertionError("explicit mode must not query the place")

    a = boot.BootArtifacts.from_env(
        {**JTAG_PL, "VRT49_CORUNDUM_BOOT": "jtag"}, resolve_mode=resolve
    )
    assert a.mode == "jtag" and a.mode_source == "VRT49_CORUNDUM_BOOT"
    a = boot.BootArtifacts.from_env(
        {
            **JTAG_PL,
            "VRT49_CORUNDUM_BOOT": "jtag",
            "HOPCAP_BOOT_SOURCE": "place tron boot-strategy=BootZynqMPJTAG",
        },
        resolve_mode=resolve,
    )
    assert a.mode_source == "place tron boot-strategy=BootZynqMPJTAG", "the wrapper resolved it"
    with pytest.raises(boot.BootConfigError, match="auto, sd or jtag"):
        boot.BootArtifacts.from_env({**JTAG_PL, "VRT49_CORUNDUM_BOOT": "usb"}, resolve_mode=resolve)


def test_an_unreadable_place_tag_is_a_boot_config_error():
    from hopcap.lab.preflight import BootModeError

    def resolve():
        raise BootModeError("place tron has no boot-strategy tag; pass --boot jtag or --boot sd")

    with pytest.raises(boot.BootConfigError, match="boot-strategy"):
        boot.BootArtifacts.from_env(JTAG_PL, resolve_mode=resolve)


def test_session_record_says_why_the_mode_ran():
    a = boot.BootArtifacts.from_env(
        JTAG_PL, resolve_mode=lambda: {"mode": "jtag", "boot_strategy": "BootZynqMPJTAG"}
    )
    rec = boot.session_record(boot.TronSession(a, log=lambda _m: None))
    assert rec["mode"] == "jtag"
    assert rec["mode_source"] == "place tron boot-strategy=BootZynqMPJTAG"
