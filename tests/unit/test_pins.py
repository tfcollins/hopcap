# SPDX-License-Identifier: MIT
"""pins.json is the fail-closed contract for every upstream the composite consumes."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHA40 = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def pins() -> dict:
    return json.loads((ROOT / "pins.json").read_text())


def _head(path: Path) -> str:
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_pins_declares_every_upstream_as_full_sha(pins):
    for key in ("tick", "vrt49", "adi_hdl", "corundum", "linux", "labgrid_plugins"):
        assert SHA40.match(pins[key]["revision"]), key


def test_submodule_heads_match_pins(pins):
    assert _head(ROOT / "third_party" / "tick") == pins["tick"]["revision"]
    assert _head(ROOT / "third_party" / "vrt49") == pins["vrt49"]["revision"]


def test_pins_copies_vrt49_platform_artifact_contract(pins):
    """ADI HDL / corundum / linux pins must equal what vrt49's CI builds against."""
    wf = (ROOT / "third_party/vrt49/.github/workflows/hdl-corundum.yml").read_text()
    assert f"HDL_SHA: {pins['adi_hdl']['revision']}" in wf
    assert f"CORUNDUM_SHA: {pins['corundum']['revision']}" in wf
    artifacts = json.loads((ROOT / "third_party/vrt49/ci/platform-artifacts.json").read_text())
    assert artifacts["linux"]["revision"] == pins["linux"]["revision"]
    zu = artifacts["platforms"]["adrv9009-zu11eg"]
    assert pins["u_boot"]["url"] == zu["u_boot_url"]
    assert pins["u_boot"]["sha256"] == zu["u_boot_sha256"]
    assert (
        pins["arm_trusted_firmware"]["revision"]
        == artifacts["arm_trusted_firmware"]["revisions"]["2025.1"]
    )
