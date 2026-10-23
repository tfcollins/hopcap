# SPDX-License-Identifier: MIT
"""Hardware fixtures for hopcap on tron. Everything skips cleanly without HOPCAP_HW=1 so the
suite collects (and `--collect-only` passes) on any host."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "src", ROOT / "third_party/tick/src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _hw() -> bool:
    return os.getenv("HOPCAP_HW") == "1"


@pytest.fixture(scope="session")
def board():
    """Boot tron with the exact artifacts named in the environment; restore and power off after."""
    if not _hw():
        pytest.skip("set HOPCAP_HW=1 inside an acquired tron reservation")
    from hopcap.lab.boot import BootArtifacts, TronSession

    artifacts = BootArtifacts.from_env(os.environ)
    session = TronSession(artifacts)
    shell = session.boot_exact()
    board_ip = shell.run(
        "ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1"
    ).strip()
    yield {"session": session, "shell": shell, "ip": board_ip, "artifacts": artifacts}
    session.restore_and_power_off()


@pytest.fixture(scope="session")
def components(board, tmp_path_factory):
    from hopcap.run import build_components_from_board

    out = Path(os.environ.get("HOPCAP_OUT", tmp_path_factory.mktemp("hopcap-run")))
    return build_components_from_board(
        board["shell"],
        board_ip=board["ip"],
        out_dir=out,
        bind=os.environ.get("HOPCAP_BIND", "192.0.2.2:4991"),
        rx_interface=os.environ.get("HOPCAP_RX_INTERFACE", "enp4s0f0np0"),
        rx_cpu=int(os.environ["HOPCAP_RX_CPU"]) if os.environ.get("HOPCAP_RX_CPU") else None,
    )
