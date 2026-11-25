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

### Profile `timed-dma` (libdatax P3b, image candidate A)

`--profile timed-dma` (SFP only) assembles the P3b image on top of the composite: Tick's
`tick_dma_gate` on the RX cpack `fifo_wr_en`/`reset` pins and between upack and the TX TPL,
`tick_fifo_tag_adapter` (with the unmodified `tick_stream_timestamp_tag`) between cpack and the
RX DMA, gate bits 0/1 level and 2/3 mode from Tick's `stream_gate_en`, the stock TX DMA →
`axi_tx_fifo` path (`VRT_DAC_TX_ENABLE=0`, recorded as MANIFEST `build_env`), and chip B's FHM
trigger on `adrv9009_gpio_00_b`. Sources: `hdl/system_bd_timed_dma.tcl`,
`hdl/system_timed_dma_cdc.xdc`, the chip-B edit in `scripts/assemble_workspace.py`.

```sh
python3 scripts/assemble_workspace.py --build-root build/ws-tdma --fetch --port sfp --profile timed-dma
```

The build exports `build_env`, skips vrt49's `check-sfp-rx-netlist.py` (it asserts cpack feeds
the RX DMA directly, which this profile replaces by design) and runs
`check_hop_netlist.py --profile timed-dma`, which re-asserts the VRT tap invariants on the TPL
nets plus the gate, tagger, TX and chip-B wiring. `verify_evidence.py bitstream` follows the
MANIFEST profile.

### Profile `adrv9361` (ADRV9361-Z7035, 1G SFP)

`--profile adrv9361` (SFP only) builds a different base: vrt49's
`hdl/projects/adrv9361z7035_vrt49_sfp` on the ADRV9361-Z7035 SOM
(design: `docs/superpowers/specs/2026-11-25-adrv9361z7035-hop-design.md`).

```sh
python3 scripts/assemble_workspace.py --build-root build/ws-a9 --fetch --profile adrv9361
scripts/build_bitstream.sh --ws build/ws-a9 --evidence evidence/bitstream-a9-<stamp>
python3 scripts/verify_evidence.py bitstream evidence/bitstream-a9-<stamp>
```

The assembler stages vrt49's project and the IP it needs plus `vrt49_time_base`, hopcap's
`vrt49_fhm_hop_ctrl`, `tick_pps_gen` and `tick_gpio_sync`, and Tick's RTL, Z7035 fragment and
CTRL_IN XDC from `third_party/tick`. Its anchored edits:

| File | Edit |
|---|---|
| `system_bd.tcl` | vrt49's own Tick (vendored, 100 MHz) + HOP1 block is cut (their IPs are not built); `source system_bd_adrv9361_hop.tcl` before `validate_bd_design` |
| `Makefile` | `LIB_DEPS` without `tick_vendor` / `vrt49_ad9361_hop_ctrl`, with `vrt49_time_base vrt49_fhm_hop_ctrl tick_pps_gen` |
| `system_project.tcl` | Tick's CTRL_IN XDC, `system_adrv9361_hop_cdc.tcl` as a late implementation-only constraint script, `check_adrv9361_hop.tcl` after vrt49's final timing gates |
| `system_top.v` | CTRL_IN[2:0] from Tick's IOB registers on `tick_clk` (read back on EMIO 42:40), `tick_clk` / `tick_ctrl_in` wrapper ports instead of `ad9361_fastlock_select` |

`hdl/system_bd_adrv9361_hop.tcl` sources Tick's fragment (scheduler at 0x44AA0000, 50 MHz from
FCLK0, IRQ ps-14), puts HOP2 at 0x44AB0000 on `sys_cpu_clk` fed `{gpio_out[2:0], gpio_out[3]}`
through `tick_gpio_sync`, drives Tick's and the time base's `pps_in` from `tick_pps_gen`, puts
`vrt49_time_base_0` on the AD9361 data clock counting the tap's `packed_fifo_wr_en` beats, and sets
`vrt49_link_throttle` `FORWARD_EVERY_N 1`.

`build_bitstream.sh` runs a plain `make` (the UDP tuple keeps vrt49's defaults; set it at runtime
through the framer's `v49_udp_*` attributes), then requires an empty `missing_external.log`, the
final routed timing and bus-skew reports clean, the post-route gate report
`hopcap_adrv9361_post_route.rpt` (CDC exceptions applied, CTRL_IN registers in OLOGIC sites) and
`check_hop_netlist.py --profile adrv9361`. It writes `system_top.bin` with
`bootgen -arch zynq -process_bitstream bin`: the board boots its stock Kuiper SD card and gets the
bitstream through the FPGA manager at runtime (no JESD on the AD9361, so a runtime PL load is
safe there, unlike the ZU11EG), followed by `kernel/dts/zynq-adrv9361z7035-hopcap-overlay.dts`
(`dtc -@`).

The overlay's time base counts beats of `packed_fifo_wr_en`. util_cpack2 packs only the enabled
converter channels, so `adi,nominal-sps = 15360000` holds at 15.36 MSPS with all four RX converter
channels (RX1 and RX2 I/Q) enabled on `cf-ad9361-lpc`; with one I/Q pair the beat rate halves and
the tap keeps every other RX1 sample.

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

With `--xsa`, the script also writes the JTAG boot inputs (`system_top.bit`, `psu_init.tcl`, via
`scripts/extract_jtag_inputs.py`) and a copy of the XSA, all listed in `SHA256SUMS`. A set with
those and no BOOT.BIN is a JTAG boot set, which `verify_evidence.py boot` accepts.

The `timed-dma` workspace (libdatax P3b, SFP only) needs the matching boot profile:

```sh
scripts/build_boot_artifacts.sh --port sfp --profile timed-dma --skip-boot-bin \
    --xsa evidence/bitstream-<stamp>/system_top.xsa --out evidence/boot-timed-dma-<stamp>
```

It uses `kernel/dts/zynqmp-adrv9009zu11eg-vrt49-control-timed-dma-overlay.dts` (vrt49's control
overlay without the deframer node, because `VRT_DAC_TX_ENABLE=0` leaves no `vrt49_dac_tx` at
0x84AB0000) and `kernel/dts/zynqmp-adrv9009zu11eg-hopcap-timed-dma-overlay.dts` (the Tick node
carries `adi,stream-gate-map`, which the Tick driver exposes as `stream_gate_map`). It skips
`patch_adrv9009_tx_dma_owner.py`, so the stock `axi-adrv9009-tx-hpc` node keeps the TX DMA. The
DTB checks assert that the deframer is absent, that the stock node still has its `dmas`, and the
exact gate map.

## Upstream dependencies carried as patches

`overlays/patches/` holds deltas the composite needs until they land upstream (vrt49
`VRT_FABRIC_HOP_ENABLE` hook, Tick `prepare_adi_hdl_adrv9009.py --system-top`). A release build
(`verify_evidence.py --release`) requires the queue to be empty.
