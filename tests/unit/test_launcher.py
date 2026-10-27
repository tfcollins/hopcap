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
    assert "HOPCAP_PLAN" in out["required_env"] and "VRT49_CORUNDUM_BOOT" in out["optional_env"]
    assert "VRT49_CORUNDUM_BOOT" not in out["required_env"], "unset means auto (the place tag)"


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


def test_auto_boot_resolves_from_the_place_tag_before_reserving():
    env = dict(BASE_ENV)
    env.pop("VRT49_CORUNDUM_BOOT", None)
    launcher.resolve_boot(env, detect=lambda: {"mode": "sd", "source": "place tron boot-mode=sd"})
    assert env["VRT49_CORUNDUM_BOOT"] == "sd"
    assert env["HOPCAP_BOOT_SOURCE"] == "place tron boot-mode=sd"
    with pytest.raises(ValueError, match="BOOT_BIN_URL"):
        launcher.require_env(env)  # mode-specific inputs are checked before the reservation

    def never():
        raise AssertionError("explicit mode must not query the place")

    env = dict(BASE_ENV, VRT49_CORUNDUM_BOOT="jtag")
    launcher.resolve_boot(env, detect=never)
    assert env["VRT49_CORUNDUM_BOOT"] == "jtag" and "HOPCAP_BOOT_SOURCE" not in env

    from hopcap.lab.preflight import BootModeError

    def unreadable():
        raise BootModeError(
            "place tron boot-mode=usb is not jtag or sd; pass --boot jtag or --boot sd"
        )

    with pytest.raises(ValueError, match="boot-mode=usb"):
        launcher.resolve_boot(dict(BASE_ENV, VRT49_CORUNDUM_BOOT="auto"), detect=unreadable)


def _run_wrapper(*args):
    return subprocess.run(
        ["bash", str(ROOT / "scripts/run-tron-hopcap.sh"), *args],
        capture_output=True,
        text=True,
    )


def _fake_python(tmp_path, answer="jtag place tron boot-mode=jtag", rc=0):
    """A --python that answers the wrapper's boot-mode query as the place tag would."""
    py = tmp_path / "fakepy"
    py.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2 $3" = "-m hopcap.lab.preflight --boot-mode" ]; then\n'
        f"  [ -n '{answer}' ] && echo '{answer}'\n"
        f"  [ {rc} -eq 0 ] || echo 'place tron boot-strategy=BootFPGASoC' >&2\n"
        f"  exit {rc}\n"
        "fi\n"
        'exec python3 "$@"\n'
    )
    py.chmod(0o755)
    return str(py)


def _base(tmp_path, **fake):
    return [
        "--artifacts",
        str(tmp_path),
        "--url",
        "http://x",
        "--out",
        str(tmp_path / "o"),
        "--python",
        _fake_python(tmp_path, **fake),
    ]


def test_operator_wrapper_takes_the_boot_mode_from_the_place_tag(tmp_path):
    r = _run_wrapper(*_base(tmp_path))  # --boot auto is the default
    assert "boot: jtag (place tron boot-mode=jtag)" in r.stdout
    assert r.returncode == 2 and "--boot jtag needs --xsa" in r.stderr
    r = _run_wrapper(*_base(tmp_path, answer="sd place tron boot-mode=sd"))
    assert "boot: sd (place tron boot-mode=sd)" in r.stdout
    assert "--xsa" not in r.stderr and "SHA256SUMS" in r.stderr  # sd never asks for an XSA


def test_operator_wrapper_fails_closed_when_the_tag_cannot_be_read(tmp_path):
    r = _run_wrapper(*_base(tmp_path, answer="", rc=2))
    assert r.returncode == 2 and "pass --boot jtag or --boot sd" in r.stderr
    assert "BootFPGASoC" in r.stderr, "the resolver's reason reaches the operator"
    assert "SHA256SUMS" not in r.stderr, "stops before touching the artifact set"


def test_operator_wrapper_explicit_boot_overrides_the_tag_with_a_warning(tmp_path):
    r = _run_wrapper(*_base(tmp_path), "--boot", "sd")
    assert "WARNING: --boot sd overrides place tron boot-mode=jtag" in r.stderr
    assert "boot: sd (--boot sd)" in r.stdout and "SHA256SUMS" in r.stderr
    assumed = "jtag no boot-mode tag on place tron; jtag assumed"
    r = _run_wrapper(*_base(tmp_path, answer=assumed), "--boot", "sd")
    assert "WARNING" not in r.stderr, "nothing to override when the tag is absent"
    r = _run_wrapper(*_base(tmp_path, answer="", rc=2), "--boot", "jtag")
    assert "boot: jtag (--boot jtag)" in r.stdout and "--boot jtag needs --xsa" in r.stderr


def test_operator_wrapper_finds_the_xsa_in_the_artifact_set(tmp_path):
    (tmp_path / "system_top.xsa").write_bytes(b"x")
    r = _run_wrapper(*_base(tmp_path))
    assert "--xsa" not in r.stderr and "SHA256SUMS" in r.stderr
    r = _run_wrapper(*_base(tmp_path), "--boot", "usb")
    assert r.returncode == 2 and "--boot must be auto, jtag or sd" in r.stderr


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
