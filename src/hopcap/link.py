# SPDX-License-Identifier: MIT
"""Which of tron's cages carries the stream, and which nemo NIC receives it.

The one place hopcap records the receiver per physical port; the framer's destination MAC, the
preflight gate and the CLI default all read it. ``VRT49_PHYSICAL_PORT`` (vrt49's variable, set by
hw-test.yml and run-tron-hopcap.sh) selects the port; the bitstream and boot set must be built for
the same one (``--port`` on assemble_workspace.py / build_bitstream.sh / build_boot_artifacts.sh).

Cabling as of 2026-11-05 (datax_sdk_sdr#25, ~/dev/infra/HOSTS.md nemo row): tron's SFP+ cage runs
to nemo's 82599 ``enp2s0``; the XL710 QSFP cage belongs to mini2's 4x10G breakout, so ``qsfp``
needs tron's QSFP DAC moved back into it."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

ENV = "VRT49_PHYSICAL_PORT"
PORTS = ("sfp", "qsfp")
DEFAULT_PORT = "sfp"


@dataclass(frozen=True)
class Receiver:
    port: str
    interface: str
    mac: str
    driver: str
    nm_profile: str  # NetworkManager profile on nemo that owns 192.0.2.2/24 on this interface


RECEIVERS = {
    "sfp": Receiver("sfp", "enp2s0", "6c:92:bf:5b:38:b9", "ixgbe", "vrt49-sfp"),
    "qsfp": Receiver("qsfp", "enp4s0f0np0", "64:9d:99:6f:a9:6a", "i40e", "vrt49-qsfp"),
}


class PortError(ValueError):
    pass


def port_from_env(env: Mapping[str, str] | None = None) -> str:
    port = (os.environ if env is None else env).get(ENV, DEFAULT_PORT).strip() or DEFAULT_PORT
    if port not in PORTS:
        raise PortError(f"{ENV}={port!r}: must be one of {', '.join(PORTS)}")
    return port


def receiver(port: str | None = None, env: Mapping[str, str] | None = None) -> Receiver:
    port = port or port_from_env(env)
    if port not in RECEIVERS:
        raise PortError(f"physical port {port!r}: must be one of {', '.join(PORTS)}")
    return RECEIVERS[port]
