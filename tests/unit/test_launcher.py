# SPDX-License-Identifier: MIT
"""examples/hardware/zu11eg_hopcap.py: offline inspection and env/JUnit validation (vrt49 convention)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "examples/hardware/zu11eg_hopcap.py"
sys.path.insert(0, str(LAUNCHER.parent))
import zu11eg_hopcap as launcher  # noqa: E402


def test_inspect_prints_pytest_and_adi_lg_argv_offline():
    r = subprocess.run(
        [sys.executable, str(LAUNCHER), "--inspect"],
        capture_output=True,
        text=True,
        env={"PATH": "", "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'third_party/tick/src'}"},
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["request"][:2] == ["adi-lg", "request"]
    assert "--part" in out["request"] and "adrv9009zu11eg" in out["request"]
    assert 'test "$LG_PLACE" = tron' in out["request"][-1]
    assert any("tests/hardware/test_hw_hopcap.py" in n for n in out["nodes"])
    assert out["lock"] == "/tmp/vrt49-corundum-hardware.lock"
    assert "HOPCAP_PLAN" in out["required_env"] and "VRT49_CORUNDUM_BOOT" in out["required_env"]


def test_validate_environment_rejects_preset_reservation_vars():
    with pytest.raises(ValueError, match="LG_ENV"):
        launcher.validate_environment({"LG_ENV": "/x"})
    launcher.validate_environment({})


def test_validate_junit_requires_every_node_and_no_failures(tmp_path):
    nodes = (
        "tests/hardware/test_hw_hopcap.py::test_boot_and_identity",
        "tests/hardware/test_hw_hopcap.py::test_hop_capture_plan",
    )
    good = tmp_path / "good.xml"
    good.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase classname="tests.hardware.test_hw_hopcap" name="test_boot_and_identity"/>'
        '<testcase classname="tests.hardware.test_hw_hopcap" name="test_hop_capture_plan"/></testsuite>'
    )
    assert launcher.validate_junit(good, nodes) == 2
    bad = tmp_path / "bad.xml"
    bad.write_text(
        '<testsuite tests="2" failures="1" errors="0" skipped="0">'
        '<testcase classname="tests.hardware.test_hw_hopcap" name="test_boot_and_identity"><failure/></testcase>'
        '<testcase classname="tests.hardware.test_hw_hopcap" name="test_hop_capture_plan" /></testsuite>'
    )
    with pytest.raises(ValueError):
        launcher.validate_junit(bad, nodes)
    skipped = tmp_path / "skip.xml"
    skipped.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="1">'
        '<testcase classname="tests.hardware.test_hw_hopcap" name="test_boot_and_identity"><skipped/></testcase></testsuite>'
    )
    with pytest.raises(ValueError):
        launcher.validate_junit(skipped, nodes[:1])


def test_hardware_suite_collects_without_a_board():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests/hardware"), "-q", "--collect-only"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "test_hop_capture_plan" in r.stdout and "test_boot_and_identity" in r.stdout
