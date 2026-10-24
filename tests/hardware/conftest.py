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
    try:
        shell = session.boot_exact()
        # Management address = the source the board uses to reach the lab LAN (nemo), never the
        # 192.0.2.1 Corundum side.  HOPCAP_BOARD_IP overrides.
        board_ip = (
            os.environ.get("HOPCAP_BOARD_IP")
            or shell.run(
                "ip -4 route get 10.0.0.71 | sed -n 's/.*src \\([0-9.]*\\).*/\\1/p' | head -1"
            ).strip()
        )
        assert board_ip and not board_ip.startswith("192.0.2."), (
            f"bad board management IP {board_ip!r}"
        )
        yield {"session": session, "shell": shell, "ip": board_ip, "artifacts": artifacts}
    finally:
        # Runs on any failure (including a failed exact boot): restore the stock /boot trio, power off.
        session.restore_and_power_off()
        import json

        from hopcap.lab.boot import session_record

        out = Path(os.environ.get("HOPCAP_OUT", "."))
        out.mkdir(parents=True, exist_ok=True)
        (out / "board-session.json").write_text(
            json.dumps(session_record(session), indent=2) + "\n"
        )


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
