# Build the bitstream and boot set

Everything is pinned by `pins.json`; `tests/unit/test_pins.py` fails if the submodules or
vrt49's own ADI HDL / Corundum / Linux pins drift from it.

## Pick the port

tron streams over one of two cages, and the bitstream, the boot set and the run must agree on it
(`--port`, recorded as `physical_port` in `MANIFEST.json` and `boot-provenance.json`):

| `--port` | Cage | Shell | GT refclk | nemo receiver |
|---|---|---|---|---|
| `sfp` (default) | SFP+ | `QSFP_ENABLE=0`, `system_top_sfp.v` | AD9545 OUT1_A/Q1A | 82599 `enp2s0` |
| `qsfp` | QSFP lane 0 | `QSFP_ENABLE=1`, `system_top_corundum.v` | AD9545 OUT1_B/Q1B | XL710 `enp4s0f0np0` |

The receiver per port lives in `src/hopcap/link.py`; runs select it with `VRT49_PHYSICAL_PORT`.
Since 2026-11-05 the XL710 cage carries mini2's 4x10G breakout, so `qsfp` runs need tron's QSFP
DAC moved back into it first (datax_sdk_sdr#25).

## 1. Assemble the workspace

```sh
git submodule update --init
python3 scripts/assemble_workspace.py --build-root build/ws --fetch [--port sfp|qsfp]
```

Or point at existing checkouts at the pinned SHAs (`--adi-hdl`, `--corundum`). The assembler
stages vrt49's IP and project, the hopcap IP and block-design fragment, Tick's RTL and fragment,
applies Tick's `patch_system_top()` to the port's top-level, edits the project files at anchored
lines, applies `overlays/patches/*.patch`, runs vrt49's dual-Talise check (plus the QSFP pinout
check for `qsfp`), and writes `MANIFEST.json` with the SHA-256 of every staged file.

## 2. Synthesize (Vivado 2025.1)

```sh
scripts/build_bitstream.sh --ws build/ws --evidence evidence/bitstream-<stamp>     # on a Vivado host
scripts/build_bitstream_remote.sh --ws build/ws --host hdl-dev-2.local               # from anywhere
python3 scripts/verify_evidence.py bitstream evidence/bitstream-<stamp> [--release]
```

The build takes the port from the workspace's `MANIFEST.json` and runs vrt49's CI invocation
(`QSFP_ENABLE=0|1`, `SFP_REFCLK_MHZ=156.25`, fixed UDP tuple), followed by vrt49's timing and RX-tap
netlist checks and `check_hop_netlist.py` on that port's top-level.

## 3. Kernel, DTB, overlays, BOOT.BIN

```sh
scripts/build_boot_artifacts.sh --port sfp --xsa evidence/bitstream-<stamp>/system_top.xsa --out evidence/boot-<stamp>
python3 scripts/verify_evidence.py boot evidence/boot-<stamp>
```

Needs `aarch64-linux-gnu-gcc`, `dtc`, `fdtoverlay`, `fdtget`; BOOT.BIN additionally needs
`xsct` and `bootgen` (pass `--skip-boot-bin` elsewhere and finish on a Vivado host). The Tick
driver is built **in-tree** (`obj-y`), so there is no module/vermagic step.

## Upstream dependencies carried as patches

`overlays/patches/` holds deltas the composite needs until they land upstream (vrt49
`VRT_FABRIC_HOP_ENABLE` hook, Tick `prepare_adi_hdl_adrv9009.py --system-top`). A release build
(`verify_evidence.py --release`) requires the queue to be empty.
