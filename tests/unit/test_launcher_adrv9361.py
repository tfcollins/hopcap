# SPDX-License-Identifier: MIT
"""examples/hardware/adrv9361_hopcap.py: offline inspection, environment guards, token-safe
place listing (tick's lablp pattern). Never touches labgrid."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "examples/hardware/adrv9361_hopcap.py"
sys.path.insert(0, str(LAUNCHER.parent))
import adrv9361_hopcap as launcher  # noqa: E402

SHOW = """Place 'lablp':
  tags: daughter-board=adrv9361z7035, carrier=adrv1crr-fmc, boot-strategy=BootFPGASoCTFTP
  matches:
    lablp.local/tlab/*
  acquired: None
  acquired resources:
Matching resource 'outlet' (lablp.local/tlab/HomeAssistantOutlet/x):
  {'token': 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc'}
"""


def test_inspect_is_offline_and_uses_the_proven_lablp_request(tmp_path):
    r = subprocess.run(
        [sys.executable, str(LAUNCHER), "--inspect"],
        capture_output=True,
        text=True,
        env={
            "PATH": "",
            "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'third_party/tick/src'}",
            "HOPCAP_OUT": str(tmp_path / "ev"),
        },
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    req = out["request"]
    assert req[:2] == ["adi-lg", "request"]
    assert req[req.index("--part") + 1] == "adrv9361z7035"
    assert req[req.index("--carrier") + 1] == "adrv1crr-fmc"
    assert req[req.index("--mode") + 1] == "uri" and "--power-down" in req
    assert req[-2] == "--run" and req[-1] == out["session"]
    assert out["session"].startswith('test -n "$IIO_URI" && HOPCAP_HW=1 HOPCAP_PLATFORM=adrv9361 ')
    assert (
        "-m hopcap.lab.lablp --evidence" in out["session"]
        and str(tmp_path / "ev") in out["session"]
    )
    assert out["place"] == "lablp" and out["lock"] is None
    assert "HOPCAP_LABLP_BIN" in out["required_env"] and "HOPCAP_PLAN" in out["optional_env"]
    assert not (tmp_path / "ev").exists()


def test_no_run_and_cpu_flags():
    inner, _ = launcher.commands(Path("/e"), 60, no_run=True, rx_cpu="4")
    assert inner.endswith("--evidence /e --no-run --rx-cpu 4")


def test_environment_guards(tmp_path):
    with pytest.raises(ValueError, match="IIO_URI"):
        launcher.validate_environment({"IIO_URI": "ip:1.2.3.4"})
    launcher.validate_environment({})
    with pytest.raises(ValueError, match="HOPCAP_LABLP_BIN"):
        launcher.require_env({"HOPCAP_OUT": "x"})
    f = tmp_path / "f"
    f.write_text("x")
    env = {"HOPCAP_OUT": "x", "HOPCAP_LABLP_BIN": str(f), "HOPCAP_TICK_MODULE": str(f)}
    with pytest.raises(ValueError, match="HOPCAP_VRT49_MODULE"):
        launcher.require_env(env)
    env["HOPCAP_VRT49_MODULE"] = str(tmp_path / "missing.ko")
    with pytest.raises(ValueError, match="not a file"):
        launcher.require_env(env)


def test_place_listing_never_keeps_the_outlet_token():
    header = launcher.place_header(SHOW)
    assert "eyJ" not in header and "token" not in header.lower()
    assert "Matching resource" not in header
    launcher.check_place(header)
    assert launcher.released(header)
    assert not launcher.released(header.replace("acquired: None", "acquired: nemo/tcollins"))
    with pytest.raises(ValueError, match="daughter-board"):
        launcher.check_place(header.replace("adrv9361z7035", "adrv9009zu11eg"))
    assert launcher.scrub("ok\nAuthorization: Bearer abc\nfine\n") == "ok\nfine\n"


def test_receiver_check_finds_enp7s0_by_mac(tmp_path):
    from hopcap import link
    from hopcap.lab import preflight

    nic = tmp_path / "enp7s0"
    (nic / "device").mkdir(parents=True)
    (nic / "address").write_text("68:05:CA:19:88:8C\n")
    (nic / "operstate").write_text("up\n")
    (tmp_path / "drivers/e1000e").mkdir(parents=True)
    (nic / "device/driver").symlink_to(tmp_path / "drivers/e1000e")
    rx = link.platform_receiver("adrv9361")
    got = preflight.receiver_interface(tmp_path, rx=rx)
    assert got["ok"] and got["interface"] == "enp7s0" and got["link_up"]
    raw = "3: enp7s0    inet 192.168.2.10/24 brd 192.168.2.255 scope global enp7s0\n"
    addr = preflight.validate_receiver_address(raw, "enp7s0", 2, expected=rx.ipv4)
    assert addr["ok"] and addr["expected"] == "192.168.2.10/24"
    assert not preflight.validate_receiver_address(raw, "enp7s0", 2)["ok"]  # tron's 192.0.2.2


def test_artifact_paths_are_resolved_before_adi_lg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {
        "HOPCAP_OUT": "ev",
        "HOPCAP_LABLP_BIN": "art/system_top.bin",
        "HOPCAP_TICK_MODULE": "~/t.ko",
        "HOPCAP_VRT49_MODULE": "/abs/v.ko",
        "HOPCAP_PLAN": "p.json",
    }
    launcher.resolve_paths(env)
    assert env["HOPCAP_LABLP_BIN"] == str(tmp_path / "art/system_top.bin")
    assert env["HOPCAP_PLAN"] == str(tmp_path / "p.json")
    assert env["HOPCAP_TICK_MODULE"] == str(Path("~/t.ko").expanduser())
    assert env["HOPCAP_VRT49_MODULE"] == "/abs/v.ko" and env["HOPCAP_OUT"] == str(tmp_path / "ev")
