# SPDX-License-Identifier: MIT
"""hopcap.link: one record of the receiver NIC per physical port, selected by VRT49_PHYSICAL_PORT."""

from __future__ import annotations

import pytest

from hopcap import link
from hopcap.framer import FramerConfig


def test_sfp_is_the_default_port():
    assert link.port_from_env({}) == "sfp"
    assert link.port_from_env({"VRT49_PHYSICAL_PORT": ""}) == "sfp"
    assert link.receiver(env={}).interface == "enp2s0"


def test_each_port_names_its_nemo_nic():
    """HOSTS.md nemo row, EEPROMs read 2026-11-05: SFP DAC in the 82599, breakout in the XL710."""
    assert link.receiver("sfp") == link.Receiver(
        "sfp", "enp2s0", "6c:92:bf:5b:38:b9", "ixgbe", "vrt49-sfp"
    )
    assert link.receiver("qsfp") == link.Receiver(
        "qsfp", "enp4s0f0np0", "64:9d:99:6f:a9:6a", "i40e", "vrt49-qsfp"
    )


def test_an_unknown_port_fails_closed():
    with pytest.raises(link.PortError, match="VRT49_PHYSICAL_PORT"):
        link.port_from_env({"VRT49_PHYSICAL_PORT": "40g"})
    with pytest.raises(link.PortError):
        link.receiver("usb")


def test_framer_destination_mac_follows_the_port(monkeypatch):
    """The 82599 drops unicast frames addressed to another MAC, so the framer must target the NIC
    that is actually cabled to the cage the bitstream drives."""
    monkeypatch.setenv("VRT49_PHYSICAL_PORT", "sfp")
    assert FramerConfig().dest_mac == "6c:92:bf:5b:38:b9"
    monkeypatch.setenv("VRT49_PHYSICAL_PORT", "qsfp")
    assert FramerConfig().dest_mac == "64:9d:99:6f:a9:6a"
    assert FramerConfig(dest_mac="02:00:00:00:00:02").dest_mac == "02:00:00:00:00:02"
