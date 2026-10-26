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


BASE_ENV = {
    "HOPCAP_PLAN": "p.json",
    "HOPCAP_OUT": "/out",
    "VRT49_CORUNDUM_KERNEL_URL": "http://x/Image",
    "VRT49_CORUNDUM_KERNEL_SHA256": "a" * 64,
    "VRT49_CORUNDUM_DTB_URL": "http://x/system.dtb",
    "VRT49_CORUNDUM_DTB_SHA256": "a" * 64,
}


def test_sd_boot_needs_boot_bin_and_jtag_boot_needs_the_pl_inputs():
    sd = dict(BASE_ENV, VRT49_CORUNDUM_BOOT="sd")
    with pytest.raises(ValueError, match="BOOT_BIN_URL"):
        launcher.require_env(sd)
    launcher.require_env(
        dict(sd, VRT49_CORUNDUM_BOOT_BIN_URL="http://x/B", VRT49_CORUNDUM_BOOT_BIN_SHA256="a" * 64)
    )
    jtag = dict(BASE_ENV, VRT49_CORUNDUM_BOOT="jtag")
    with pytest.raises(ValueError, match=r"BITSTREAM_REMOTE.*PSU_INIT_REMOTE"):
        launcher.require_env(jtag)
    launcher.require_env(
        dict(
            jtag,
            VRT49_CORUNDUM_BITSTREAM_REMOTE="/tmp/j/system_top.bit",
            VRT49_CORUNDUM_PSU_INIT_REMOTE="/tmp/j/psu_init.tcl",
        )
    )


def _run_wrapper(*args):
    return subprocess.run(
        ["bash", str(ROOT / "scripts/run-tron-hopcap.sh"), *args],
        capture_output=True,
        text=True,
    )


def test_operator_wrapper_jtag_boot_needs_an_xsa_and_rejects_unknown_modes(tmp_path):
    base = ["--artifacts", str(tmp_path), "--url", "http://x", "--out", str(tmp_path / "o")]
    r = _run_wrapper(*base, "--boot", "jtag")
    assert r.returncode == 2 and "--boot jtag needs --xsa" in r.stderr
    r = _run_wrapper(*base)  # jtag is the default since tron became JTAG-strapped (2026-10-25)
    assert r.returncode == 2 and "--boot jtag needs --xsa" in r.stderr
    r = _run_wrapper(*base, "--boot", "sd")
    assert "--xsa" not in r.stderr and "SHA256SUMS" in r.stderr  # sd never asks for an XSA


def test_operator_wrapper_finds_the_xsa_in_the_artifact_set(tmp_path):
    (tmp_path / "system_top.xsa").write_bytes(b"x")
    base = ["--artifacts", str(tmp_path), "--url", "http://x", "--out", str(tmp_path / "o")]
    r = _run_wrapper(*base)
    assert "--xsa" not in r.stderr and "SHA256SUMS" in r.stderr
    r = _run_wrapper(*base, "--boot", "usb")
    assert r.returncode == 2 and "--boot must be sd or jtag" in r.stderr


def test_jtag_inputs_come_from_the_xsa(tmp_path):
    import zipfile

    xsa = tmp_path / "system_top.xsa"
    with zipfile.ZipFile(xsa, "w") as z:
        z.writestr("system_top.bit", b"BIT")
        z.writestr("psu_init.tcl", "proc psu_init {} {}\n")
    out = tmp_path / "jtag"
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/extract_jtag_inputs.py"), str(xsa), str(out)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert (out / "system_top.bit").read_bytes() == b"BIT"
    assert "proc psu_init" in (out / "psu_init.tcl").read_text()
    sums = r.stdout.split()
    import hashlib

    assert hashlib.sha256(b"BIT").hexdigest() in sums
    with zipfile.ZipFile(xsa, "w") as z:
        z.writestr("system_top.bit", b"BIT")
        z.writestr("psu_init.tcl", "# empty\n")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/extract_jtag_inputs.py"), str(xsa), str(out)],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and "psu_init" in r.stderr
