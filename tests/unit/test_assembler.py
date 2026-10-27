# SPDX-License-Identifier: MIT
"""assemble_workspace.py stages vrt49 + hopcap + Tick into a pinned ADI HDL tree, idempotently,
with a per-file hash manifest. Exercised against a synthetic ADI tree holding the anchors the
patches need."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import assemble_workspace as aw  # noqa: E402

VRT49 = ROOT / "third_party/vrt49"
TICK = ROOT / "third_party/tick"
PROJECT_REL = Path("projects/adrv9009zu11eg_vrt49_corundum")
ADI_PROJECT_REL = Path("projects/adrv9009zu11eg/adrv2crr_fmc")

SYSTEM_TOP_CORUNDUM = """// synthetic excerpt of ADI system_top_corundum.v carrying the Tick patch anchors
module system_top (
  inout  [92:0] gpio_bd
);
  wire [94:0] gpio_i; wire [94:0] gpio_o; wire [94:0] gpio_t;
  ad_iobuf #(
    .DATA_WIDTH(61)
  ) i_iobuf (
    .dio_t ({gpio_t[92:32]}),
    .dio_i ({gpio_o[92:32]}),
    .dio_o ({gpio_i[92:32]}),
    .dio_p ({adrv9009_gpio_18_b,
              adrv9009_gpio_01_a,       // 33
              adrv9009_gpio_00_a}));    // 32

  system_wrapper i_system_wrapper (
    .gpio_i (gpio_i));
endmodule
"""


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def fake_tree(tmp_path: Path) -> dict:
    adi = tmp_path / "hdl"
    (adi / ADI_PROJECT_REL).mkdir(parents=True)
    (adi / ADI_PROJECT_REL / "system_top_corundum.v").write_text(SYSTEM_TOP_CORUNDUM)
    (adi / "library").mkdir()
    (adi / "library/corundum").mkdir()
    (adi / "library/corundum/patch_axis_xgmii_rx_64.patch").write_text("")
    subprocess.run(["git", "init", "-q", str(adi)], check=True)
    subprocess.run(
        ["git", "-C", str(adi), "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(adi),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "x",
        ],
        check=True,
    )
    corundum = tmp_path / "corundum"
    (corundum / "fpga").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(corundum)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(corundum),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    return {"adi": adi, "corundum": corundum, "root": tmp_path}


def test_assemble_stages_everything_and_patches_the_project(fake_tree):
    adi = fake_tree["adi"]
    manifest = aw.assemble(
        fake_tree["root"],
        adi_hdl=adi,
        corundum=fake_tree["corundum"],
        check_pins=False,
        contract_checks=False,
    )
    # vrt49 IP + project staged
    assert (adi / "library/axi_vrt49_framer/axi_vrt49_framer.v").exists()
    assert (adi / "library/vrt49_udp_tx").is_dir()
    assert (adi / PROJECT_REL / "system_bd_vrt49.tcl").exists()
    # composite IP + fragment
    assert (adi / "library/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v").read_text() == (
        ROOT / "hdl/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v"
    ).read_text()
    assert (adi / PROJECT_REL / "system_bd_fabric_hop.tcl").read_text() == (
        ROOT / "hdl/system_bd_fabric_hop.tcl"
    ).read_text()
    # Tick RTL + fragment where Tick's own tooling expects them
    assert (adi / "library/axi_timed_command_scheduler/axi_timed_command_scheduler.v").exists()
    assert (adi / ADI_PROJECT_REL / "tick_adrv9009zu11eg_adrv2crr_fmc.tcl").exists()
    # system_top_corundum.v patched exactly like Tick does for system_top.v
    top = (adi / ADI_PROJECT_REL / "system_top_corundum.v").read_text()
    assert "assign adrv9009_gpio_00_a = tick_fhm_gpio[0];" in top
    assert ".DATA_WIDTH(60)" in top and ".tick_fhm_gpio (tick_fhm_gpio)," in top
    # project edits
    sp = (adi / PROJECT_REL / "system_project.tcl").read_text()
    assert '"../../library/axi_timed_command_scheduler/axi_timed_command_scheduler.v"' in sp
    bd = (adi / PROJECT_REL / "system_bd.tcl").read_text()
    assert (
        bd.index("source system_bd_vrt49.tcl")
        < bd.index("tick_adrv9009zu11eg_adrv2crr_fmc.tcl")
        < bd.index("source system_bd_fabric_hop.tcl")
    )
    mk = (adi / PROJECT_REL / "Makefile").read_text()
    assert (
        "LIB_DEPS += vrt49_fhm_hop_ctrl" in mk
        and "LIB_DEPS += axi_timed_command_scheduler" not in mk
    )
    assert "M_DEPS += system_bd_fabric_hop.tcl" in mk
    # manifest
    assert manifest["pins"]["adi_hdl"]["revision"]
    assert manifest["files"]["library/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v"]
    assert manifest["files"][str(ADI_PROJECT_REL / "system_top_corundum.v")]
    assert (fake_tree["root"] / "MANIFEST.json").exists()


def test_assemble_is_idempotent(fake_tree):
    m1 = aw.assemble(
        fake_tree["root"],
        adi_hdl=fake_tree["adi"],
        corundum=fake_tree["corundum"],
        check_pins=False,
        contract_checks=False,
    )
    m2 = aw.assemble(
        fake_tree["root"],
        adi_hdl=fake_tree["adi"],
        corundum=fake_tree["corundum"],
        check_pins=False,
        contract_checks=False,
    )
    assert m1["files"] == m2["files"]
    bd = (fake_tree["adi"] / PROJECT_REL / "system_bd.tcl").read_text()
    assert bd.count("source system_bd_fabric_hop.tcl") == 1
    assert bd.count("tick_adrv9009zu11eg_adrv2crr_fmc.tcl") == 1


def test_pin_check_fails_closed_on_wrong_adi_sha(fake_tree):
    with pytest.raises(aw.AssembleError, match="adi_hdl"):
        aw.assemble(
            fake_tree["root"],
            adi_hdl=fake_tree["adi"],
            corundum=fake_tree["corundum"],
            check_pins=True,
            contract_checks=False,
        )


def test_submodule_pins_are_verified_before_staging(fake_tree, monkeypatch):
    bad = json.loads((ROOT / "pins.json").read_text())
    bad["tick"]["revision"] = "0" * 40
    monkeypatch.setattr(aw, "load_pins", lambda: bad)
    with pytest.raises(aw.AssembleError, match="tick"):
        aw.assemble(
            fake_tree["root"],
            adi_hdl=fake_tree["adi"],
            corundum=fake_tree["corundum"],
            check_pins=False,
            contract_checks=False,
        )


def test_patch_queue_is_applied_and_recorded(fake_tree, tmp_path, monkeypatch):
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "0001-note.patch").write_text(
        "--- a/projects/adrv9009zu11eg_vrt49_corundum/README.hopcap\n"
        "+++ b/projects/adrv9009zu11eg_vrt49_corundum/README.hopcap\n"
        "@@ -0,0 +1 @@\n+patched by hopcap queue\n"
    )
    monkeypatch.setattr(aw, "PATCH_DIR", patches)
    m = aw.assemble(
        fake_tree["root"],
        adi_hdl=fake_tree["adi"],
        corundum=fake_tree["corundum"],
        check_pins=False,
        contract_checks=False,
    )
    assert (
        fake_tree["adi"] / PROJECT_REL / "README.hopcap"
    ).read_text().strip() == "patched by hopcap queue"
    assert m["patch_queue"] == ["0001-note.patch"]


def test_cli_dry_run_lists_actions_without_touching_tree(fake_tree):
    before = sorted(
        p.relative_to(fake_tree["adi"])
        for p in fake_tree["adi"].rglob("*")
        if ".git" not in p.parts
    )
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/assemble_workspace.py"),
            "--build-root",
            str(fake_tree["root"]),
            "--adi-hdl",
            str(fake_tree["adi"]),
            "--corundum",
            str(fake_tree["corundum"]),
            "--no-pin-check",
            "--no-contract-checks",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    after = sorted(
        p.relative_to(fake_tree["adi"])
        for p in fake_tree["adi"].rglob("*")
        if ".git" not in p.parts
    )
    assert before == after
    assert "vrt49_fhm_hop_ctrl" in r.stdout
