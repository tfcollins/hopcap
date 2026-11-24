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


PORT_TOPS = {
    "sfp": PROJECT_REL / "system_top_sfp.v",
    "qsfp": ADI_PROJECT_REL / "system_top_corundum.v",
}


@pytest.mark.parametrize("port", ["sfp", "qsfp"])
def test_assemble_stages_everything_and_patches_the_project(fake_tree, port):
    adi = fake_tree["adi"]
    manifest = aw.assemble(
        fake_tree["root"],
        adi_hdl=adi,
        corundum=fake_tree["corundum"],
        check_pins=False,
        contract_checks=False,
        port=port,
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
    # Tick pps_in selector IP + its CDC constraints
    for name in ("tick_pps_select.v", "tick_pps_select_ip.tcl", "Makefile"):
        assert (adi / "library/tick_pps_select" / name).read_bytes() == (
            ROOT / "hdl/tick_pps_select" / name
        ).read_bytes()
    assert (adi / PROJECT_REL / "system_tick_pps_cdc.xdc").read_text() == (
        ROOT / "hdl/system_tick_pps_cdc.xdc"
    ).read_text()
    # Tick RTL + fragment where Tick's own tooling expects them
    assert (adi / "library/axi_timed_command_scheduler/axi_timed_command_scheduler.v").exists()
    assert (adi / ADI_PROJECT_REL / "tick_adrv9009zu11eg_adrv2crr_fmc.tcl").exists()
    # the top-level this port builds is patched exactly like Tick does for system_top.v, and
    # only that one: the other port's top must stay as its source tree has it
    top = (adi / PORT_TOPS[port]).read_text()
    assert "assign adrv9009_gpio_00_a = tick_fhm_gpio[0];" in top
    assert ".DATA_WIDTH(60)" in top and ".tick_fhm_gpio (tick_fhm_gpio)," in top
    other = adi / PORT_TOPS["qsfp" if port == "sfp" else "sfp"]
    assert "tick_fhm_gpio" not in other.read_text()
    # project edits
    sp = (adi / PROJECT_REL / "system_project.tcl").read_text()
    assert '"../../library/axi_timed_command_scheduler/axi_timed_command_scheduler.v"' in sp
    # the PPS CDC xdc is in the project file list, right after vrt49's own CDC constraints
    assert sp.index('"system_vrt49_cdc.xdc"') < sp.index('"system_tick_pps_cdc.xdc"')
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
    assert "LIB_DEPS += tick_pps_select" in mk and "M_DEPS += system_tick_pps_cdc.xdc" in mk
    # manifest
    assert manifest["pins"]["adi_hdl"]["revision"]
    assert manifest["files"]["library/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v"]
    assert manifest["files"]["library/tick_pps_select/tick_pps_select.v"] == aw.sha256(
        ROOT / "hdl/tick_pps_select/tick_pps_select.v"
    )
    assert manifest["files"][str(PROJECT_REL / "system_tick_pps_cdc.xdc")]
    assert manifest["files"][str(PORT_TOPS[port])]
    assert manifest["physical_port"] == port
    assert json.loads((fake_tree["root"] / "MANIFEST.json").read_text())["physical_port"] == port


def test_unknown_port_is_refused(fake_tree):
    with pytest.raises(aw.AssembleError, match="port"):
        aw.assemble(
            fake_tree["root"],
            adi_hdl=fake_tree["adi"],
            corundum=fake_tree["corundum"],
            check_pins=False,
            contract_checks=False,
            port="usb",
        )


@pytest.mark.parametrize(
    ("port", "expected"),
    [
        ("sfp", ["configure_dual_talise_links.py"]),
        ("qsfp", ["configure_dual_talise_links.py", "check-zu11eg-qsfp-contract.py"]),
    ],
)
def test_contract_checks_follow_the_port(monkeypatch, tmp_path, port, expected):
    """The QSFP pinout check would fail on an SFP build's staged tree; it runs only for qsfp."""
    calls = []

    def run(argv, **kw):
        calls.append(Path(argv[1]).name)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(aw.subprocess, "run", run)
    assert aw.run_contract_checks(tmp_path, port) == expected
    assert calls == expected


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
    sp = (fake_tree["adi"] / PROJECT_REL / "system_project.tcl").read_text()
    assert sp.count('"system_tick_pps_cdc.xdc"') == 1
    assert sp.count("axi_timed_command_scheduler.v") == 1
    mk = (fake_tree["adi"] / PROJECT_REL / "Makefile").read_text()
    assert mk.count("LIB_DEPS += tick_pps_select") == 1


def test_project_edits_are_idempotent_and_upgrade_older_patches():
    """Re-running the edits adds nothing; a tree patched before the PPS selector existed gains
    only the selector's entries, never a second copy of the whole block."""
    sp = '  "system_vrt49_cdc.xdc" \\\n  "other.tcl" ]]\n'
    once = aw.patch_system_project(sp)
    assert aw.patch_system_project(once) == once
    old_sp = sp.replace(
        '"system_vrt49_cdc.xdc" \\', '"system_vrt49_cdc.xdc" \\\n  ' + aw.TICK_RTL_ENTRY + " \\"
    )
    upgraded = aw.patch_system_project(old_sp)
    assert upgraded.count(aw.TICK_RTL_ENTRY) == 1 and upgraded.count(aw.PPS_CDC_ENTRY) == 1

    mk = "PROJECT_NAME := x\ninclude ../scripts/project-xilinx.mk\n"
    once = aw.patch_makefile(mk)
    assert aw.patch_makefile(once) == once
    old_mk = once.replace("M_DEPS += system_tick_pps_cdc.xdc\n", "").replace(
        "LIB_DEPS += tick_pps_select\n", ""
    )
    upgraded = aw.patch_makefile(old_mk)
    assert upgraded.count("LIB_DEPS += tick_pps_select") == 1
    assert upgraded.count("M_DEPS += system_tick_pps_cdc.xdc") == 1
    assert upgraded.count("# hopcap additions") == 1


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


# ---------------------------------------------------------------- timed-dma profile (P3b G1)

# vrt49 >= 7b11305 closes the first project file list after its CDC xdc and splices in the
# opt-in feature constraints; the pre-#81 layout keeps a trailing backslash.
NEW_SP = (
    '  "$ad_hdl_dir/library/common/ad_iobuf.v"] $physical_files [list \\\n'
    '  "system_vrt49_cdc.xdc"] $vrt49_feature_cdc_files [list \\\n'
    '  "other.tcl" ]]\n'
    "set_property strategy Performance_ExploreWithRemap [get_runs impl_1]\n\n"
    "adi_project_run adrv9009zu11eg_vrt49_corundum\n"
)


def test_project_edit_accepts_the_feature_cdc_layout():
    once = aw.patch_system_project(NEW_SP)
    assert aw.patch_system_project(once) == once
    # still one Tcl list element each, after vrt49's CDC xdc and before the splice
    assert (
        '  "system_vrt49_cdc.xdc" \\\n  '
        + aw.TICK_RTL_ENTRY
        + " \\\n  "
        + aw.PPS_CDC_ENTRY
        + "] $vrt49_feature_cdc_files [list \\"
    ) in once
    partial = NEW_SP.replace(
        '"system_vrt49_cdc.xdc"]', '"system_vrt49_cdc.xdc" \\\n  ' + aw.TICK_RTL_ENTRY + "]"
    )
    upgraded = aw.patch_system_project(partial)
    assert upgraded.count(aw.TICK_RTL_ENTRY) == 1 and upgraded.count(aw.PPS_CDC_ENTRY) == 1
    with pytest.raises(aw.AssembleError, match="system_vrt49_cdc"):
        aw.patch_system_project(NEW_SP.replace("system_vrt49_cdc.xdc", "other_cdc.xdc"))


def test_tdma_project_edit_adds_the_cdc_xdc_for_implementation_only():
    once = aw.patch_system_project_tdma(NEW_SP)
    assert aw.patch_system_project_tdma(once) == once
    assert once.index(aw.PPS_CDC_ENTRY) < once.index(aw.TDMA_CDC_ENTRY)
    assert once.index(aw.TDMA_CDC_ENTRY) < once.index("$vrt49_feature_cdc_files")
    assert once.index(aw.TDMA_CDC_IMPL_ONLY) < once.index("adi_project_run")


def test_tdma_cdc_xdc_constrains_core_clk_a_at_its_real_period():
    """ADI declares tx_dev_clk (core_clk_a) at 4.00 ns; Tick on it needs the real 8.138 ns."""
    xdc = (ROOT / "hdl" / aw.TDMA_CDC_XDC).read_text()
    lines = [ln.split("#")[0].strip() for ln in xdc.splitlines()]
    assert "create_clock -name tx_dev_clk -period 8.138 [get_ports core_clk_a_p]" in lines
    # sysref_a's input delays are bound to the redefined clock object, so they are repeated
    for edge in ("-max", "-min"):
        assert f"set_input_delay -clock tx_dev_clk {edge} 4 [get_ports sysref_a_p]" in lines
    # read after ADI's adrv9009zu11eg_constr.xdc, which declares the 4.00 ns clock
    sp = (VRT49 / "hdl/projects/adrv9009zu11eg_vrt49_corundum/system_project.tcl").read_text()
    once = aw.patch_system_project_tdma(aw.patch_system_project(sp))
    assert once.index("adrv9009zu11eg_constr.xdc") < once.index(aw.TDMA_CDC_ENTRY)


def test_tdma_profile_is_sfp_only(fake_tree):
    with pytest.raises(aw.AssembleError, match="sfp"):
        aw.assemble(
            fake_tree["root"],
            adi_hdl=fake_tree["adi"],
            corundum=fake_tree["corundum"],
            check_pins=False,
            contract_checks=False,
            port="qsfp",
            profile="timed-dma",
        )
    with pytest.raises(aw.AssembleError, match="profile"):
        aw.assemble(
            fake_tree["root"],
            adi_hdl=fake_tree["adi"],
            corundum=fake_tree["corundum"],
            check_pins=False,
            contract_checks=False,
            profile="nope",
        )


def test_tdma_assembly_stages_the_profile_and_routes_chip_b(fake_tree):
    adi = fake_tree["adi"]
    manifest = aw.assemble(
        fake_tree["root"],
        adi_hdl=adi,
        corundum=fake_tree["corundum"],
        check_pins=False,
        contract_checks=False,
        port="sfp",
        profile="timed-dma",
    )
    assert manifest["profile"] == "timed-dma"
    assert manifest["build_env"] == {"VRT_DAC_TX_ENABLE": "0", "TICK_TIMEBASE_CLK": "core_clk_a"}
    for rel in aw.TDMA_TICK_RTL:
        assert (adi / rel).read_bytes() == (TICK / "hdl" / rel).read_bytes()
        assert manifest["files"][str(rel)] == aw.sha256(TICK / "hdl" / rel)
    for rel in aw.TDMA_HOP_RTL:
        src = ROOT / "hdl" / rel.relative_to("library")
        assert (adi / rel).read_bytes() == src.read_bytes()
        assert manifest["files"][str(rel)] == aw.sha256(src)
    for name in (aw.TDMA_FRAGMENT, aw.TDMA_CDC_XDC):
        assert (adi / PROJECT_REL / name).read_bytes() == (ROOT / "hdl" / name).read_bytes()
        assert manifest["files"][str(PROJECT_REL / name)]
    bd = (adi / PROJECT_REL / "system_bd.tcl").read_text()
    assert bd.index("source system_bd_fabric_hop.tcl") < bd.index(aw.TDMA_FRAGMENT_SOURCE)
    sp = (adi / PROJECT_REL / "system_project.tcl").read_text()
    assert sp.count(aw.TDMA_CDC_ENTRY) == 1 and aw.TDMA_CDC_IMPL_ONLY in sp
    mk = (adi / PROJECT_REL / "Makefile").read_text()
    assert f"M_DEPS += {aw.TDMA_FRAGMENT}" in mk and f"M_DEPS += {aw.TDMA_CDC_XDC}" in mk
    assert "M_DEPS += ../../library/tick_gpio_sync/tick_gpio_sync.v" in mk

    # chip B on the real vrt49 SFP top, after Tick's chip-A edit
    top = (adi / PORT_TOPS["sfp"]).read_text()
    assert "assign adrv9009_gpio_00_a = tick_fhm_gpio[0];" in top
    assert top.index("wire   [ 3:0]   tick_fhm_gpio;") < top.index(aw.CHIP_B_MARKER)
    assert "assign gpio_i[58] = tick_fhm_gpio[1];" in top
    import re

    bufs = {}
    for m in re.finditer(
        r"\.DATA_WIDTH\((\d+)\)\s*\) (\w+) \(\s*\.dio_t \(\{gpio_t\[(\d+):(\d+)\]\}\),"
        r"\s*\.dio_i \(\{gpio_o\[(\d+):(\d+)\]\}\),\s*\.dio_o \(\{gpio_i\[(\d+):(\d+)\]\}\),"
        r"\s*\.dio_p \(\{(.*?)\}\)\);",
        top,
        re.S,
    ):
        pins = re.findall(r"^\s*(\w+)", re.sub(r"//[^\n]*", "", m[9]), re.M)
        assert m[3] == m[5] == m[7] and m[4] == m[6] == m[8], m[2]
        bufs[(int(m[3]), int(m[4]))] = (int(m[1]), pins)
    hi, lo = bufs[(92, 59)], bufs[(57, 33)]
    assert hi[0] == len(hi[1]) == 34 and lo[0] == len(lo[1]) == 25
    # MSB first: the last entry is the lowest bit
    assert hi[1][0] == "gpio_4_exp_n" and hi[1][-1] == "adrv9009_gpio_01_b"
    assert lo[1][0] == "adrv9009_tx2_enable_a" and lo[1][-1] == "adrv9009_gpio_01_a"
    every = [p for _, pins in bufs.values() for p in pins]
    assert "adrv9009_gpio_00_b" not in every and "adrv9009_gpio_00_a" not in every
    # idempotent
    m2 = aw.assemble(
        fake_tree["root"],
        adi_hdl=adi,
        corundum=fake_tree["corundum"],
        check_pins=False,
        contract_checks=False,
        port="sfp",
        profile="timed-dma",
    )
    assert m2["files"] == manifest["files"]
    assert (adi / PORT_TOPS["sfp"]).read_text().count(aw.CHIP_B_MARKER) == 1


def test_chip_b_patch_fails_closed_on_an_unexpected_top():
    with pytest.raises(aw.AssembleError, match="chip-B"):
        aw.patch_system_top_chip_b("module system_top; endmodule\n")


def test_default_profile_manifest_records_no_build_env(fake_tree):
    m = aw.assemble(
        fake_tree["root"],
        adi_hdl=fake_tree["adi"],
        corundum=fake_tree["corundum"],
        check_pins=False,
        contract_checks=False,
    )
    assert m["profile"] == "default" and m["build_env"] == {}
    assert not any("tick_dma_gate" in f for f in m["files"])
