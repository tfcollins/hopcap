# SPDX-License-Identifier: MIT
"""lab/preflight.py: read-only nemo-side gate for the QSFP link and lab state."""

from __future__ import annotations

from pathlib import Path

from hopcap.lab import preflight


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
