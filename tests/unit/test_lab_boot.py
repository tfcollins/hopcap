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
