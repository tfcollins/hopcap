#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Assemble a pinned ADI HDL + Corundum workspace with vrt49, Tick and hopcap staged in.

Codifies the "Create pinned ADI HDL and Corundum workspace" and "Stage VRT49 IP" steps of
vrt49/.github/workflows/hdl-corundum.yml, then adds what the composite needs:

  library/vrt49_fhm_hop_ctrl/                       hopcap IP
  library/axi_timed_command_scheduler/              Tick RTL + IP tcl (module reference)
  projects/adrv9009zu11eg/adrv2crr_fmc/tick_*.tcl   Tick fragment, where Tick's tooling expects it
  projects/adrv9009zu11eg/adrv2crr_fmc/system_top_corundum.v   Tick's patch_system_top() (TAL_GPIO_0)
  projects/adrv9009zu11eg_vrt49_corundum/           vrt49 project + system_bd_fabric_hop.tcl, with
                                                    anchored edits to system_project.tcl / system_bd.tcl / Makefile
  overlays/patches/*.patch                          pre-upstream deltas (must be empty for a release build)
  MANIFEST.json                                     SHA-256 of every staged file + pins

Usage:
  assemble_workspace.py --build-root build/ws [--adi-hdl PATH] [--corundum PATH] [--fetch] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINS_PATH = ROOT / "pins.json"
PATCH_DIR = ROOT / "overlays" / "patches"
VRT49 = ROOT / "third_party" / "vrt49"
TICK = ROOT / "third_party" / "tick"

VRT49_IPS = (
    "vrt49_rx_filter",
    "axi_vrt49_framer",
    "axi_vrt49_deframer",
    "vrt49_unpack",
    "vrt49_dac_tx",
    "util_fifo_wr_to_axis",
    "vrt49_record_capture",
    "vrt49_counter_cdc",
    "vrt49_time_base",
    "vrt49_pack",
    "vrt49_axis_to_network",
    "vrt49_frame_throttle",
    "vrt49_udp_tx",
    "axis_frame_arb_mux_2",
    "vrt49_rocev2_eth_gateway",
    "vrt49_rocev2_cdc",
    "vrt49_rocev2_control",
    "vrt49_rocev2_iq_capture",
    "vrt49_rocev2_streamer",
    "vrt49_rocev2_diag_snapshot",
)
VRT49_CORUNDUM_ETH_SFP = Path("hdl/corundum/ethernet/adrv9009zu11eg_sfp")
PROJECT_REL = Path("projects/adrv9009zu11eg_vrt49_corundum")
ADI_PROJECT_REL = Path("projects/adrv9009zu11eg/adrv2crr_fmc")
TICK_LIB_REL = Path("library/axi_timed_command_scheduler")
HOP_LIB_REL = Path("library/vrt49_fhm_hop_ctrl")

TICK_RTL_ENTRY = '"../../library/axi_timed_command_scheduler/axi_timed_command_scheduler.v"'
TICK_FRAGMENT_SOURCE = (
    "source $ad_hdl_dir/projects/adrv9009zu11eg/adrv2crr_fmc/tick_adrv9009zu11eg_adrv2crr_fmc.tcl"
)
HOP_FRAGMENT_SOURCE = "source system_bd_fabric_hop.tcl"


class AssembleError(RuntimeError):
    pass


def load_pins() -> dict:
    return json.loads(PINS_PATH.read_text())


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def log(msg: str) -> None:
    print(f"assemble: {msg}")


# ---------------------------------------------------------------------------
# pins
# ---------------------------------------------------------------------------


def verify_submodules(pins: dict) -> None:
    for name, sub in (("tick", TICK), ("vrt49", VRT49)):
        head = git(sub, "rev-parse", "HEAD")
        if head != pins[name]["revision"]:
            raise AssembleError(f"submodule {name} at {head} != pins.json {pins[name]['revision']}")


def verify_checkout(name: str, path: Path, pins: dict) -> None:
    head = git(path, "rev-parse", "HEAD")
    if head != pins[name]["revision"]:
        raise AssembleError(
            f"{name} checkout {path} at {head} != pins.json {pins[name]['revision']}"
        )


def fetch_pinned(name: str, dest: Path, pins: dict) -> Path:
    repo, sha = pins[name]["repository"], pins[name]["revision"]
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(dest)], check=True)
        subprocess.run(["git", "-C", str(dest), "remote", "add", "origin", repo], check=True)
    for attempt in range(1, 5):
        r = subprocess.run(
            ["git", "-C", str(dest), "fetch", "--depth=1", "origin", sha],
            env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin"},
        )
        if r.returncode == 0:
            break
        log(f"fetch {name} attempt {attempt} failed")
    else:
        raise AssembleError(f"could not fetch {name} {sha} from {repo}")
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", "--detach", "FETCH_HEAD"], check=True)
    return dest


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------


def stage_dir(src: Path, dst: Path, dry: bool) -> None:
    log(f"stage {src.relative_to(ROOT) if src.is_relative_to(ROOT) else src} -> {dst}")
    if dry:
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "sim_build*"))


def stage_file(src: Path, dst: Path, dry: bool) -> None:
    log(f"copy {src.name} -> {dst}")
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def edit(path: Path, fn, dry: bool) -> None:
    text = path.read_text()
    new = fn(text)
    if new != text:
        log(f"edit {path}")
        if not dry:
            path.write_text(new)


def patch_system_project(text: str) -> str:
    marker = '  "system_vrt49_cdc.xdc" \\'
    if marker not in text:
        raise AssembleError("system_project.tcl: anchor 'system_vrt49_cdc.xdc' not found")
    if TICK_RTL_ENTRY in text:
        return text
    return text.replace(marker, marker + f"\n  {TICK_RTL_ENTRY} \\", 1)


def patch_system_bd(text: str) -> str:
    if "source system_bd_vrt49.tcl" not in text:
        raise AssembleError("system_bd.tcl: anchor 'source system_bd_vrt49.tcl' not found")
    if TICK_FRAGMENT_SOURCE in text and HOP_FRAGMENT_SOURCE in text:
        return text
    lines = text.rstrip("\n").split("\n")
    idx = next(i for i, ln in enumerate(lines) if ln.strip() == "source system_bd_vrt49.tcl")
    insert = []
    if TICK_FRAGMENT_SOURCE not in text:
        insert += [
            "# hopcap: Tick scheduler (axi_timed_command_scheduler_0 @0x46000000, tick_fhm_gpio)",
            TICK_FRAGMENT_SOURCE,
        ]
    if HOP_FRAGMENT_SOURCE not in text:
        insert += [
            "# hopcap: vrt49_fhm_hop_ctrl_0 @0x84AC0000 between Tick and the framer",
            HOP_FRAGMENT_SOURCE,
        ]
    lines[idx + 1 : idx + 1] = insert
    return "\n".join(lines) + "\n"


def patch_makefile(text: str) -> str:
    if "LIB_DEPS += vrt49_fhm_hop_ctrl" in text:
        return text
    anchor = "include ../scripts/project-xilinx.mk"
    if anchor not in text:
        raise AssembleError("Makefile: anchor 'include ../scripts/project-xilinx.mk' not found")
    block = (
        "# hopcap additions\n"
        "M_DEPS += system_bd_fabric_hop.tcl\n"
        "M_DEPS += ../adrv9009zu11eg/adrv2crr_fmc/tick_adrv9009zu11eg_adrv2crr_fmc.tcl\n"
        "M_DEPS += ../../library/axi_timed_command_scheduler/axi_timed_command_scheduler.v\n"
        "LIB_DEPS += vrt49_fhm_hop_ctrl\n\n"
    )
    return text.replace(anchor, block + anchor, 1)


def apply_patch_queue(adi: Path, dry: bool) -> list[str]:
    applied = []
    for patch in sorted(PATCH_DIR.glob("*.patch")) if PATCH_DIR.exists() else []:
        log(f"apply {patch.name}")
        applied.append(patch.name)
        if dry:
            continue
        r = subprocess.run(
            ["patch", "-p1", "-N", "-s", "-d", str(adi), "-i", str(patch)],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise AssembleError(f"patch {patch.name} failed: {r.stdout}{r.stderr}")
    return applied


def staged_files(adi: Path) -> list[Path]:
    out: list[Path] = []
    for rel in (
        *(Path("library") / ip for ip in VRT49_IPS),
        Path("library/corundum/ethernet/adrv9009zu11eg_sfp"),
        PROJECT_REL,
        TICK_LIB_REL,
        HOP_LIB_REL,
        ADI_PROJECT_REL / "tick_adrv9009zu11eg_adrv2crr_fmc.tcl",
        ADI_PROJECT_REL / "system_top_corundum.v",
    ):
        p = adi / rel
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(q for q in sorted(p.rglob("*")) if q.is_file())
    return out


def run_contract_checks(adi: Path) -> list[str]:
    """The two checks vrt49 CI runs on the staged tree (dual Talise links, QSFP pinout)."""
    ran = []
    for script, args in (
        (VRT49 / "hdl" / PROJECT_REL / "configure_dual_talise_links.py", [str(adi)]),
        (VRT49 / "scripts" / "check-zu11eg-qsfp-contract.py", [str(adi)]),
    ):
        r = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise AssembleError(f"{script.name} failed:\n{r.stdout}{r.stderr}")
        log(f"{script.name}: {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else 'ok'}")
        ran.append(script.name)
    return ran


def assemble(
    build_root: Path,
    *,
    adi_hdl: Path | None = None,
    corundum: Path | None = None,
    check_pins: bool = True,
    fetch: bool = False,
    dry_run: bool = False,
    contract_checks: bool = True,
) -> dict:
    pins = load_pins()
    verify_submodules(pins)
    build_root = Path(build_root)
    build_root.mkdir(parents=True, exist_ok=True)

    adi = Path(adi_hdl) if adi_hdl else build_root / "hdl"
    cor = Path(corundum) if corundum else build_root / "corundum"
    if fetch:
        if not dry_run:
            fetch_pinned("adi_hdl", adi, pins)
            fetch_pinned("corundum", cor, pins)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(cor),
                    "apply",
                    str(adi / "library/corundum/patch_axis_xgmii_rx_64.patch"),
                ],
                check=False,
            )
    for name, path in (("adi_hdl", adi), ("corundum", cor)):
        if not (path / ".git").exists():
            raise AssembleError(
                f"{name} checkout missing at {path} (pass --fetch or --{name.replace('_', '-')})"
            )
        if check_pins:
            verify_checkout(name, path, pins)

    # vrt49 IP + project (as hdl-corundum.yml stages them)
    for ip in VRT49_IPS:
        stage_dir(VRT49 / "hdl" / ip, adi / "library" / ip, dry_run)
    stage_dir(
        VRT49 / VRT49_CORUNDUM_ETH_SFP,
        adi / "library/corundum/ethernet/adrv9009zu11eg_sfp",
        dry_run,
    )
    stage_dir(VRT49 / "hdl" / PROJECT_REL, adi / PROJECT_REL, dry_run)

    # hopcap IP + fragment
    stage_dir(ROOT / "hdl" / "vrt49_fhm_hop_ctrl", adi / HOP_LIB_REL, dry_run)
    stage_file(
        ROOT / "hdl" / "system_bd_fabric_hop.tcl",
        adi / PROJECT_REL / "system_bd_fabric_hop.tcl",
        dry_run,
    )

    # Tick: RTL + IP tcl + fragment, then its own system_top patch on the Corundum top
    tick_lib = TICK / "hdl" / TICK_LIB_REL
    stage_file(
        tick_lib / "axi_timed_command_scheduler.v",
        adi / TICK_LIB_REL / "axi_timed_command_scheduler.v",
        dry_run,
    )
    stage_file(
        tick_lib / "axi_timed_command_scheduler_ip.tcl",
        adi / TICK_LIB_REL / "axi_timed_command_scheduler_ip.tcl",
        dry_run,
    )
    stage_file(
        TICK / "hdl" / ADI_PROJECT_REL / "tick_adrv9009zu11eg_adrv2crr_fmc.tcl",
        adi / ADI_PROJECT_REL / "tick_adrv9009zu11eg_adrv2crr_fmc.tcl",
        dry_run,
    )
    sys.path.insert(0, str(TICK / "scripts"))
    import prepare_adi_hdl_adrv9009 as tick_prep

    top = adi / ADI_PROJECT_REL / "system_top_corundum.v"
    if not top.exists():
        raise AssembleError(
            f"{top} missing: ADI tree is not a Corundum-capable adrv2crr_fmc project"
        )
    log(f"patch_system_top {top}")
    if not dry_run:
        try:
            tick_prep.patch_system_top(top)
        except SystemExit as exc:  # Tick raises SystemExit on missing anchors
            raise AssembleError(str(exc)) from exc

    # anchored edits to the staged vrt49 project
    if not dry_run:
        edit(adi / PROJECT_REL / "system_project.tcl", patch_system_project, dry_run)
        edit(adi / PROJECT_REL / "system_bd.tcl", patch_system_bd, dry_run)
        edit(adi / PROJECT_REL / "Makefile", patch_makefile, dry_run)

    queue = apply_patch_queue(adi, dry_run)
    checks = run_contract_checks(adi) if (contract_checks and not dry_run) else []

    manifest = {
        "schema": "hopcap-workspace-manifest/v1",
        "pins": pins,
        "submodules": {
            "tick": git(TICK, "rev-parse", "HEAD"),
            "vrt49": git(VRT49, "rev-parse", "HEAD"),
        },
        "adi_hdl_head": git(adi, "rev-parse", "HEAD"),
        "corundum_head": git(cor, "rev-parse", "HEAD"),
        "patch_queue": queue,
        "contract_checks": checks,
        "files": {} if dry_run else {str(p.relative_to(adi)): sha256(p) for p in staged_files(adi)},
    }
    if not dry_run:
        (build_root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
        log(f"wrote {build_root / 'MANIFEST.json'} ({len(manifest['files'])} files)")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--build-root", type=Path, default=ROOT / "build" / "ws")
    ap.add_argument(
        "--adi-hdl",
        type=Path,
        help="existing ADI HDL checkout at pins.json adi_hdl (default: build-root/hdl)",
    )
    ap.add_argument(
        "--corundum", type=Path, help="existing Corundum checkout (default: build-root/corundum)"
    )
    ap.add_argument(
        "--fetch",
        action="store_true",
        help="shallow-fetch the pinned ADI HDL and Corundum into build-root",
    )
    ap.add_argument("--no-pin-check", action="store_true")
    ap.add_argument(
        "--no-contract-checks", action="store_true", help="skip vrt49's dual-Talise/QSFP checks"
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        assemble(
            args.build_root,
            adi_hdl=args.adi_hdl,
            corundum=args.corundum,
            check_pins=not args.no_pin_check,
            fetch=args.fetch,
            dry_run=args.dry_run,
            contract_checks=not args.no_contract_checks,
        )
    except AssembleError as exc:
        print(f"assemble: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
