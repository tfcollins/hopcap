# SPDX-License-Identifier: MIT
"""lab/preflight.py: read-only nemo-side gate for the QSFP link and lab state."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hopcap.lab import preflight

# Shape of `labgrid-client -p tron show`: the resource params after the place section carry the
# Home Assistant bearer token, which must never reach any output.
SHOW = (
    "Place 'tron':\n"
    "  comment: ADRV9009-ZU11EG (dual Talise) on ADRV2CRR-FMC (JTAG boot via tron exporter; no SD mux)\n"
    "  tags: board-location=tron, {mode}boot-strategy={strategy}, carrier=adrv2crr-fmc, "
    "daughter-board=adrv9009zu11eg, runner=hw-nemo\n"
    "  matches:\n    tron/tlab/HomeAssistantOutlet/HomeAssistantOutlet\n"
    "  acquired: None\n"
    "Matching resource 'HomeAssistantOutlet' (tron/tlab/HomeAssistantOutlet/HomeAssistantOutlet):\n"
    "  {{'params': {{'entity_id': 'switch.x', 'token': 'SECRET-HA-TOKEN'}}}}\n"
)


def fake_net(tmp_path: Path, *, mac="64:9d:99:6f:a9:6a", driver="i40e", operstate="up") -> Path:
    root = tmp_path / "net"
    iface = root / "enp4s0f0np0"
    (iface / "device").mkdir(parents=True)
    (iface / "address").write_text(mac + "\n")
    (iface / "operstate").write_text(operstate + "\n")
    (iface / "device/driver").symlink_to(tmp_path / f"drivers/{driver}")
    (tmp_path / f"drivers/{driver}").mkdir(parents=True)
    (iface / "device/vendor").write_text("0x8086\n")
    (root / "lo").mkdir()
    (root / "lo/address").write_text("00:00:00:00:00:00\n")
    return root


def test_receiver_interface_found_by_mac_and_driver(tmp_path):
    root = fake_net(tmp_path)
    r = preflight.receiver_interface(root)
    assert (
        r["ok"]
        and r["interface"] == "enp4s0f0np0"
        and r["driver"] == "i40e"
        and r["operstate"] == "up"
    )


def test_receiver_interface_fails_closed_on_missing_mac_or_link_down(tmp_path):
    root = fake_net(tmp_path, mac="00:11:22:33:44:55")
    assert preflight.receiver_interface(root)["ok"] is False
    root = fake_net(tmp_path / "b", operstate="down")
    r = preflight.receiver_interface(root)
    assert r["ok"] is True and r["link_up"] is False, "link down is reported, not gated"
    root = fake_net(tmp_path / "c", driver="ixgbe")
    assert preflight.receiver_interface(root)["ok"] is False


def test_address_check_requires_receiver_interface_or_rp_filter_zero():
    on_rx = "5: enp4s0f0np0    inet 192.0.2.2/24 brd 192.0.2.255 scope global enp4s0f0np0\n"
    assert preflight.validate_receiver_address(on_rx, "enp4s0f0np0", 2)["ok"]
    other = "2: enp2s0    inet 192.0.2.2/24 brd 192.0.2.255 scope global enp2s0\n"
    assert preflight.validate_receiver_address(other, "enp4s0f0np0", 0)["ok"]
    r = preflight.validate_receiver_address(other, "enp4s0f0np0", 2)
    assert not r["ok"] and "rp_filter" in r["note"], (
        "address-less NIC + rp_filter!=0 drops everything (measured on nemo)"
    )
    assert not preflight.validate_receiver_address(
        "2: enp2s0 inet 10.0.0.5/22 brd ...\n", "enp4s0f0np0", 0
    )["ok"]


def test_place_tags_are_matched_exactly():
    raw = (
        "Place 'tron':\n  tags: boot-strategy=BootZynqMPJTAG, carrier=adrv2crr-fmc, "
        "daughter-board=adrv9009zu11eg, runner=hw-nemo\n  matches:\n    nemo/tron/*\n  acquired: None\n"
    )
    r = preflight.validate_place(raw)
    assert r["ok"] and r["acquired"] is None
    assert not preflight.validate_place(raw.replace("adrv9009zu11eg", "ad9081"))["ok"]


def test_lock_state_reports_holder(tmp_path):
    lock = tmp_path / "lock"
    assert preflight.lock_state(lock)["free"] is True
    import fcntl

    with open(lock, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert preflight.lock_state(lock)["free"] is False


def test_summary_is_ok_only_when_all_sections_ok():
    sections = {
        "receiver": {"ok": True},
        "address": {"ok": True},
        "place": {"ok": True},
        "lock": {"free": True},
        "coordinator": {"ok": True},
    }
    assert preflight.summarize(sections)["ok"]
    sections["lock"]["free"] = False
    assert not preflight.summarize(sections)["ok"]


def show(strategy="BootZynqMPJTAG", mode=None):
    return SHOW.format(strategy=strategy, mode=f"boot-mode={mode}, " if mode else "")


def test_boot_mode_follows_the_place_boot_mode_tag():
    assert preflight.boot_mode_from_show(show(mode="jtag")) == {
        "mode": "jtag",
        "source": "place tron boot-mode=jtag",
        "boot_strategy": "BootZynqMPJTAG",
    }
    assert preflight.boot_mode_from_show(show(mode="sd"))["mode"] == "sd"


def test_without_a_boot_mode_tag_jtag_is_assumed_and_said_so():
    r = preflight.boot_mode_from_show(show())
    assert r["mode"] == "jtag" and r["source"].startswith("no boot-mode tag on place tron")


def test_boot_strategy_must_stay_bootzynqmpjtag_in_both_modes():
    """adi-lg renders LG_ENV from the boot-strategy tag; hopcap drives both boot paths through the
    BootZynqMPJTAG driver, so that tag says nothing about the straps."""
    for strategy in ("BootFPGASoC", "BootFabric"):
        with pytest.raises(preflight.BootModeError, match="BootZynqMPJTAG") as e:
            preflight.boot_mode_from_show(show(strategy, mode="sd"))
        assert strategy in str(e.value) and "SECRET" not in str(e.value)


def test_boot_mode_fails_closed_on_a_bad_tag_or_another_place():
    with pytest.raises(preflight.BootModeError, match="--boot"):
        preflight.boot_mode_from_show(show(mode="usb"))
    with pytest.raises(preflight.BootModeError, match="tron"):
        preflight.boot_mode_from_show(show().replace("'tron'", "'mini2'"))
    with pytest.raises(preflight.BootModeError):
        preflight.boot_mode_from_show("")


def test_validate_place_reports_the_boot_mode_without_gating_on_it():
    r = preflight.validate_place(show(mode="sd"))
    assert r["ok"] and r["boot_strategy"] == "BootZynqMPJTAG" and r["boot_mode"] == "sd"
    r = preflight.validate_place(show())
    assert r["ok"] and r["boot_mode"] is None
    assert "SECRET" not in repr(r)


def test_detect_boot_mode_strips_lg_vars_and_finds_the_client_beside_python(tmp_path, monkeypatch):
    for k in ("LG_ENV", "LG_STATE", "LG_PLACE"):
        monkeypatch.setenv(k, "set-by-a-reservation")
    (tmp_path / "bin").mkdir()
    client = tmp_path / "bin/labgrid-client"
    client.write_text("#!/bin/sh\n")
    client.chmod(0o755)
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: None)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin/python"))
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout=show(mode="jtag"))

    assert preflight.detect_boot_mode(run=run)["mode"] == "jtag"
    argv, kw = calls[0]
    assert argv == [str(client), "-p", "tron", "show"]
    assert kw["env"]["LG_COORDINATOR"] == "10.0.0.41:20408"
    assert not [k for k in kw["env"] if k.startswith("LG_") and k != "LG_COORDINATOR"], (
        "LG_ENV/LG_STATE can activate a strategy even for `show`"
    )
    assert os.environ["LG_ENV"] == "set-by-a-reservation", "caller environment untouched"

    def failing(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="")

    with pytest.raises(preflight.BootModeError, match="show failed"):
        preflight.detect_boot_mode(run=failing)


def test_boot_mode_cli_prints_only_the_mode_and_its_source(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "find_labgrid_client", lambda: "labgrid-client")

    def run_with(raw):
        def run(argv, **kw):
            return SimpleNamespace(returncode=0, stdout=raw)

        monkeypatch.setattr(preflight.subprocess, "run", run)

    run_with(show(mode="sd"))
    assert preflight.main(["--boot-mode"]) == 0
    out = capsys.readouterr()
    assert out.out == "sd place tron boot-mode=sd\n" and "SECRET" not in out.out + out.err
    run_with(show("BootFPGASoC", mode="sd"))
    assert preflight.main(["--boot-mode"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "boot-strategy=BootZynqMPJTAG" in out.err and "SECRET" not in out.err
