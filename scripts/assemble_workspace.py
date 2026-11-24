#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Assemble a pinned ADI HDL + Corundum workspace with vrt49, Tick and hopcap staged in.

Codifies the "Create pinned ADI HDL and Corundum workspace" and "Stage VRT49 IP" steps of
vrt49/.github/workflows/hdl-corundum.yml, then adds what the composite needs:

  library/vrt49_fhm_hop_ctrl/                       hopcap IP
  library/tick_pps_select/                          hopcap IP: Tick pps_in selector (ext PPS / Corundum PPS)
  library/axi_timed_command_scheduler/              Tick RTL + IP tcl (module reference)
  projects/adrv9009zu11eg/adrv2crr_fmc/tick_*.tcl   Tick fragment, where Tick's tooling expects it
  <port top>.v                                      Tick's patch_system_top() (TAL_GPIO_0) on the top the
                                                    --port build uses: the vrt49 project's system_top_sfp.v
                                                    (sfp, default) or ADI's adrv2crr_fmc/system_top_corundum.v (qsfp)
  projects/adrv9009zu11eg_vrt49_corundum/           vrt49 project + system_bd_fabric_hop.tcl +
                                                    system_tick_pps_cdc.xdc, with
                                                    anchored edits to system_project.tcl / system_bd.tcl / Makefile
  overlays/patches/*.patch                          pre-upstream deltas (must be empty for a release build)
  MANIFEST.json                                     SHA-256 of every staged file + pins + profile

Profiles (``--profile``, recorded in MANIFEST.json with the build environment it needs):

  default    the hopcap composite above
  timed-dma  libdatax P3b image candidate A (spike gate G1), SFP only. Adds:
             * build_env VRT_DAC_TX_ENABLE=0 (vrt49 keeps the stock TX DMA -> axi_tx_fifo nets);
               build_bitstream.sh exports MANIFEST build_env before make
             * Tick's tick_dma_gate, tick_fifo_tag_adapter and tick_stream_timestamp_tag RTL
               under library/ (module references, added by the fragment)
             * system_bd_timed_dma.tcl (RX/TX gates, RX tagger; sourced after the hop fragment)
               and system_timed_dma_cdc.xdc (implementation-only CDC exceptions)
             * chip B's FHM trigger on the port top: the Talise GPIO IOBUF is split into
               [92:59] + [57:33] and adrv9009_gpio_00_b / gpio_i[58] = tick_fhm_gpio[1]

Usage:
  assemble_workspace.py --build-root build/ws [--port sfp|qsfp] [--profile default|timed-dma]
                        [--adi-hdl PATH] [--corundum PATH] [--fetch] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
PPS_LIB_REL = Path("library/tick_pps_select")
PPS_CDC_XDC = "system_tick_pps_cdc.xdc"

TICK_RTL_ENTRY = '"../../library/axi_timed_command_scheduler/axi_timed_command_scheduler.v"'
PPS_CDC_ENTRY = f'"{PPS_CDC_XDC}"'
TICK_FRAGMENT_SOURCE = (
    "source $ad_hdl_dir/projects/adrv9009zu11eg/adrv2crr_fmc/tick_adrv9009zu11eg_adrv2crr_fmc.tcl"
)
HOP_FRAGMENT_SOURCE = "source system_bd_fabric_hop.tcl"
PORTS = ("sfp", "qsfp")
# The top-level each QSFP_ENABLE value builds (vrt49 system_project.tcl): Tick patches that one.
PORT_TOP = {
    "sfp": PROJECT_REL / "system_top_sfp.v",
    "qsfp": ADI_PROJECT_REL / "system_top_corundum.v",
}

# --- timed-dma profile (libdatax P3b candidate A) ---------------------------------------
TDMA_FRAGMENT = "system_bd_timed_dma.tcl"
TDMA_CDC_XDC = "system_timed_dma_cdc.xdc"
TDMA_FRAGMENT_SOURCE = f"source {TDMA_FRAGMENT}"
TDMA_CDC_ENTRY = f'"{TDMA_CDC_XDC}"'
TDMA_CDC_IMPL_ONLY = f"set_property USED_IN_SYNTHESIS false [get_files *{TDMA_CDC_XDC}]"
# Tick RTL the fragment instantiates as module references (the adapter wraps the tagger).
TDMA_TICK_RTL = (
    Path("library/tick_dma_gate/tick_dma_gate.v"),
    Path("library/tick_stream_timestamp_tag/tick_stream_timestamp_tag.v"),
    Path("library/tick_fifo_tag_adapter/tick_fifo_tag_adapter.v"),
)
PROFILES = {
    "default": {"build_env": {}, "ports": PORTS},
    "timed-dma": {"build_env": {"VRT_DAC_TX_ENABLE": "0"}, "ports": ("sfp",)},
}

# Chip B FHM (P3b decision 7, spike G4): applied after Tick's patch_system_top, which already
# narrowed the Talise GPIO IOBUF to gpio[92:33] and declared tick_fhm_gpio. Bit 58 is
# adrv9009_gpio_00_b; the IOBUF is split around it rather than narrowed.
CHIP_B_EDITS = (
    (
        "    .DATA_WIDTH(60)\n"
        "  ) i_iobuf (\n"
        "    .dio_t ({gpio_t[92:33]}),\n"
        "    .dio_i ({gpio_o[92:33]}),\n"
        "    .dio_o ({gpio_i[92:33]}),\n",
        "    .DATA_WIDTH(34)\n"
        "  ) i_iobuf (\n"
        "    .dio_t ({gpio_t[92:59]}),\n"
        "    .dio_i ({gpio_o[92:59]}),\n"
        "    .dio_o ({gpio_i[92:59]}),\n",
    ),
    (
        "              adrv9009_gpio_01_b,       // 58\n"
        "              adrv9009_gpio_00_b,       // 58\n",
        "              adrv9009_gpio_01_b}));    // 59\n"
        "\n"
        "  // hopcap timed-dma: Talise B ARM GPIO 0 (adrv9009_gpio_00_b, EMIO gpio[58]) is chip B's\n"
        "  // FHM trigger, driven by tick_fhm_gpio[1] below. The IOBUF is split around bit 58.\n"
        "  ad_iobuf #(\n"
        "    .DATA_WIDTH(25)\n"
        "  ) i_iobuf_tick_fhm_b (\n"
        "    .dio_t ({gpio_t[57:33]}),\n"
        "    .dio_i ({gpio_o[57:33]}),\n"
        "    .dio_o ({gpio_i[57:33]}),\n"
        "    .dio_p ({\n",
    ),
    (
        "  assign gpio_i[32] = tick_fhm_gpio[0];\n",
        "  assign gpio_i[32] = tick_fhm_gpio[0];\n"
        "\n"
        "  // hopcap timed-dma: chip B FHM trigger (Talise B TAL_GPIO_00), mirrored into EMIO.\n"
        "  assign adrv9009_gpio_00_b = tick_fhm_gpio[1];\n"
        "  assign gpio_i[58] = tick_fhm_gpio[1];\n",
    ),
)
CHIP_B_MARKER = "assign adrv9009_gpio_00_b = tick_fhm_gpio[1];"


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


def apply_corundum_patch(adi: Path, cor: Path) -> None:
    """ADI's Corundum patch, as vrt49 CI applies it. The path is absolute because `git -C`
    resolves a relative one against the Corundum checkout (a relative --build-root used to
    skip the patch silently); an already-applied patch is accepted, any other failure is not."""
    patch = str((adi / "library/corundum/patch_axis_xgmii_rx_64.patch").resolve())
    git_apply = ["git", "-C", str(cor), "apply"]
    if subprocess.run([*git_apply, "--reverse", "--check", patch], capture_output=True).returncode:
        r = subprocess.run([*git_apply, patch], capture_output=True, text=True)
        if r.returncode != 0:
            raise AssembleError(f"corundum patch {patch} failed: {r.stderr.strip()}")
        log(f"applied {Path(patch).name} to {cor}")
    else:
        log(f"{Path(patch).name} already applied to {cor}")


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


# vrt49 >= 7b11305 (#81) closes the first file list right after its CDC xdc and splices in the opt-in
# feature constraints: `"system_vrt49_cdc.xdc"] $vrt49_feature_cdc_files [list \`.
_FEATURE_CDC_SPLICE = re.compile(r'^(  "[^"\n]+")\] \$vrt49_feature_cdc_files \[list \\$', re.M)


def patch_system_project(text: str, extra: tuple[str, ...] = ()) -> str:
    """Add hopcap's project files right after vrt49's own CDC constraints, in either layout."""
    entries = [e for e in (TICK_RTL_ENTRY, PPS_CDC_ENTRY, *extra) if e not in text]
    marker = '  "system_vrt49_cdc.xdc" \\'
    if marker in text:
        added = "".join(f"\n  {entry} \\" for entry in entries)
        return text.replace(marker, marker + added, 1) if added else text
    m = _FEATURE_CDC_SPLICE.search(text)
    if m is None or '"system_vrt49_cdc.xdc"' not in text[: m.end(1)]:
        raise AssembleError("system_project.tcl: anchor 'system_vrt49_cdc.xdc' not found")
    if not entries:
        return text
    added = "".join(f" \\\n  {entry}" for entry in entries)
    return text[: m.end(1)] + added + text[m.end(1) :]


def patch_system_project_tdma(text: str) -> str:
    """timed-dma: the CDC xdc names cells inside OOC module references, so implementation only."""
    text = patch_system_project(text, (TDMA_CDC_ENTRY,))
    if TDMA_CDC_IMPL_ONLY in text:
        return text
    anchor = "\nadi_project_run adrv9009zu11eg_vrt49_corundum"
    if anchor not in text:
        raise AssembleError("system_project.tcl: anchor 'adi_project_run' not found")
    return text.replace(anchor, f"\n# hopcap timed-dma\n{TDMA_CDC_IMPL_ONLY}\n{anchor}", 1)


def patch_system_bd_tdma(text: str) -> str:
    """timed-dma: source the gate/tagger fragment right after the hop fragment."""
    if TDMA_FRAGMENT_SOURCE in text:
        return text
    lines = text.rstrip("\n").split("\n")
    idx = next((i for i, ln in enumerate(lines) if ln.strip() == HOP_FRAGMENT_SOURCE), None)
    if idx is None:
        raise AssembleError(f"system_bd.tcl: anchor {HOP_FRAGMENT_SOURCE!r} not found")
    lines[idx + 1 : idx + 1] = [
        "# hopcap timed-dma: Tick DMA gates (RX/TX) + RX tagger (libdatax P3b)",
        TDMA_FRAGMENT_SOURCE,
    ]
    return "\n".join(lines) + "\n"


def patch_makefile_tdma(text: str) -> str:
    marker = "# hopcap timed-dma profile\n"
    if marker in text:
        return text
    anchor = "include ../scripts/project-xilinx.mk"
    if anchor not in text:
        raise AssembleError("Makefile: anchor 'include ../scripts/project-xilinx.mk' not found")
    block = marker + "".join(
        f"M_DEPS += {dep}\n"
        for dep in (TDMA_FRAGMENT, TDMA_CDC_XDC, *(f"../../{p}" for p in TDMA_TICK_RTL))
    )
    return text.replace(anchor, block + "\n" + anchor, 1)


def patch_system_top_chip_b(text: str) -> str:
    """Chip B FHM on a top Tick has already patched (see CHIP_B_EDITS)."""
    if CHIP_B_MARKER in text:
        return text
    for old, new in CHIP_B_EDITS:
        if text.count(old) != 1:
            raise AssembleError(
                f"chip-B FHM patch: expected one {old.splitlines()[0].strip()!r} block in the "
                "Tick-patched top-level; the IOBUF layout changed"
            )
        text = text.replace(old, new, 1)
    return text


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
    if "LIB_DEPS += vrt49_fhm_hop_ctrl" in text and "LIB_DEPS += tick_pps_select" in text:
        return text
    if "LIB_DEPS += vrt49_fhm_hop_ctrl\n" in text:  # patched before the PPS selector existed
        return text.replace(
            "LIB_DEPS += vrt49_fhm_hop_ctrl\n",
            f"LIB_DEPS += vrt49_fhm_hop_ctrl\nM_DEPS += {PPS_CDC_XDC}\nLIB_DEPS += tick_pps_select\n",
            1,
        )
    anchor = "include ../scripts/project-xilinx.mk"
    if anchor not in text:
        raise AssembleError("Makefile: anchor 'include ../scripts/project-xilinx.mk' not found")
    block = (
        "# hopcap additions\n"
        "M_DEPS += system_bd_fabric_hop.tcl\n"
        f"M_DEPS += {PPS_CDC_XDC}\n"
        "M_DEPS += ../adrv9009zu11eg/adrv2crr_fmc/tick_adrv9009zu11eg_adrv2crr_fmc.tcl\n"
        "M_DEPS += ../../library/axi_timed_command_scheduler/axi_timed_command_scheduler.v\n"
        "LIB_DEPS += vrt49_fhm_hop_ctrl\n"
        "LIB_DEPS += tick_pps_select\n\n"
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


def staged_files(adi: Path, port: str = "sfp", profile: str = "default") -> list[Path]:
    out: list[Path] = []
    tdma = (
        (*TDMA_TICK_RTL, PROJECT_REL / TDMA_FRAGMENT, PROJECT_REL / TDMA_CDC_XDC)
        if profile == "timed-dma"
        else ()
    )
    for rel in (
        *(Path("library") / ip for ip in VRT49_IPS),
        Path("library/corundum/ethernet/adrv9009zu11eg_sfp"),
        PROJECT_REL,
        TICK_LIB_REL,
        HOP_LIB_REL,
        PPS_LIB_REL,
        ADI_PROJECT_REL / "tick_adrv9009zu11eg_adrv2crr_fmc.tcl",
        PORT_TOP[port],
        *tdma,
    ):
        p = adi / rel
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(q for q in sorted(p.rglob("*")) if q.is_file())
    return out


def run_contract_checks(adi: Path, port: str = "sfp") -> list[str]:
    """The checks vrt49 CI runs on the staged tree: dual Talise links always, the QSFP pinout
    only for a QSFP build (the SFP shell's contracts are vrt49 unit tests on its own tree)."""
    ran = []
    checks = [(VRT49 / "hdl" / PROJECT_REL / "configure_dual_talise_links.py", [str(adi)])]
    if port == "qsfp":
        checks.append((VRT49 / "scripts" / "check-zu11eg-qsfp-contract.py", [str(adi)]))
    for script, args in checks:
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
    port: str = "sfp",
    profile: str = "default",
) -> dict:
    if port not in PORTS:
        raise AssembleError(f"port must be one of {PORTS}, not {port!r}")
    if profile not in PROFILES:
        raise AssembleError(f"profile must be one of {tuple(PROFILES)}, not {profile!r}")
    if port not in PROFILES[profile]["ports"]:
        raise AssembleError(
            f"profile {profile} supports --port {'/'.join(PROFILES[profile]['ports'])} only, "
            f"not {port}"
        )
    tdma = profile == "timed-dma"
    pins = load_pins()
    verify_submodules(pins)
    build_root = Path(build_root)
    build_root.mkdir(parents=True, exist_ok=True)

    adi = Path(adi_hdl) if adi_hdl else build_root / "hdl"
    cor = Path(corundum) if corundum else build_root / "corundum"
    if fetch and not dry_run:
        fetch_pinned("adi_hdl", adi, pins)
        fetch_pinned("corundum", cor, pins)
        apply_corundum_patch(adi, cor)
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

    # hopcap IP + fragment + the PPS selector's CDC constraints
    stage_dir(ROOT / "hdl" / "vrt49_fhm_hop_ctrl", adi / HOP_LIB_REL, dry_run)
    stage_dir(ROOT / "hdl" / "tick_pps_select", adi / PPS_LIB_REL, dry_run)
    stage_file(
        ROOT / "hdl" / "system_bd_fabric_hop.tcl",
        adi / PROJECT_REL / "system_bd_fabric_hop.tcl",
        dry_run,
    )
    stage_file(ROOT / "hdl" / PPS_CDC_XDC, adi / PROJECT_REL / PPS_CDC_XDC, dry_run)
    if tdma:
        for name in (TDMA_FRAGMENT, TDMA_CDC_XDC):
            stage_file(ROOT / "hdl" / name, adi / PROJECT_REL / name, dry_run)
        for rel in TDMA_TICK_RTL:
            stage_file(TICK / "hdl" / rel, adi / rel, dry_run)

    # Tick: RTL + IP tcl + fragment, then its own system_top patch on the port's top
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

    top = adi / PORT_TOP[port]
    if not top.exists() and not dry_run:
        raise AssembleError(f"{top} missing: no {port} top-level to carry the Tick FHM GPIO")
    log(f"patch_system_top {top}")
    if not dry_run:
        try:
            tick_prep.patch_system_top(top)
        except SystemExit as exc:  # Tick raises SystemExit on missing anchors
            raise AssembleError(str(exc)) from exc
    if tdma:
        log(f"chip-B FHM patch {top}")
        if not dry_run:
            edit(top, patch_system_top_chip_b, dry_run)

    # anchored edits to the staged vrt49 project
    if not dry_run:
        edit(
            adi / PROJECT_REL / "system_project.tcl",
            patch_system_project_tdma if tdma else patch_system_project,
            dry_run,
        )
        edit(adi / PROJECT_REL / "system_bd.tcl", patch_system_bd, dry_run)
        edit(adi / PROJECT_REL / "Makefile", patch_makefile, dry_run)
        if tdma:
            edit(adi / PROJECT_REL / "system_bd.tcl", patch_system_bd_tdma, dry_run)
            edit(adi / PROJECT_REL / "Makefile", patch_makefile_tdma, dry_run)

    queue = apply_patch_queue(adi, dry_run)
    checks = run_contract_checks(adi, port) if (contract_checks and not dry_run) else []

    manifest = {
        "schema": "hopcap-workspace-manifest/v1",
        "physical_port": port,
        "profile": profile,
        "build_env": dict(PROFILES[profile]["build_env"]),
        "pins": pins,
        "submodules": {
            "tick": git(TICK, "rev-parse", "HEAD"),
            "vrt49": git(VRT49, "rev-parse", "HEAD"),
        },
        "adi_hdl_head": git(adi, "rev-parse", "HEAD"),
        "corundum_head": git(cor, "rev-parse", "HEAD"),
        "patch_queue": queue,
        "contract_checks": checks,
        "files": {}
        if dry_run
        else {str(p.relative_to(adi)): sha256(p) for p in staged_files(adi, port, profile)},
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
        "--port",
        choices=PORTS,
        default="sfp",
        help="cage the bitstream drives: sfp (SFP+ cage, default) or qsfp (QSFP lane 0)",
    )
    ap.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default="default",
        help="default (hopcap composite) or timed-dma (libdatax P3b candidate A; sfp only)",
    )
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
        "--no-contract-checks", action="store_true", help="skip vrt49's staged-tree checks"
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
            port=args.port,
            profile=args.profile,
        )
    except AssembleError as exc:
        print(f"assemble: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
