# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A composite example repo: Tick (hard-timed FHM GPIO hop) + vrt49 (VITA 49.2 over 10G SFP+/QSFP) on the
ADRV9009-ZU11EG, joined by a new FPGA block `vrt49_fhm_hop_ctrl` that gates capture after a settle
delay and stamps the hop frequency + epoch into a hardware-emitted Context packet. Design document:
`docs/superpowers/specs/2026-10-23-hopcap-design.md` (read it before touching the architecture).

A second platform, the ADRV9361-Z7035 (AD9361, 1G SFP, place `lablp`), is the `adrv9361` profile
of `scripts/assemble_workspace.py`: vrt49's `adrv9361z7035_vrt49_sfp` + Tick's Z7035 fragment at
50 MHz + HOP2 + an internal 1PPS (`hdl/tick_pps_gen`), hopping RX and TX through AD9361 FastLock
pin select. Design: `docs/superpowers/specs/2026-11-25-adrv9361z7035-hop-design.md`. Files:
`hdl/system_bd_adrv9361_hop.tcl`, `hdl/system_adrv9361_hop_cdc.tcl`, `hdl/check_adrv9361_hop.tcl`
(post-route gate), `kernel/dts/zynq-adrv9361z7035-hopcap-overlay.dts`.

Tick and vrt49 are **submodules**, pinned by `pins.json`. `tests/unit/test_pins.py` fails if the
submodule HEADs or vrt49's own ADI HDL / Corundum / Linux pins drift from `pins.json`. Bump pins
deliberately, in one commit, with the reason in `pins.json`'s `why` field.

## Commands

```sh
git submodule update --init
pip install -e third_party/tick -e ".[test]"
pytest -q                                   # offline unit tests (tests/unit)
pytest tests/hardware -q --collect-only     # hardware suite must collect without a board

# cocotb (1.9.x; 2.x breaks Verilator < 5.036) — venv lives in hdl/sim/.venv
uv venv hdl/sim/.venv && uv pip install --python hdl/sim/.venv/bin/python cocotb==1.9.2 pytest
make -C hdl/sim/cocotb TOP=vrt49_fhm_hop_ctrl     # controller unit suite
make -C hdl/sim/cocotb TOP=fhm_hop_chain          # controller + vrt49 framer, decoded with vrt49_codec
make -C hdl/sim/cocotb TOP=tick_pps_gen           # adrv9361 internal 1PPS
make -C hdl/sim/cocotb lint                       # Verilator --lint-only

# adrv9361 workspace (needs ADI HDL + Corundum at the pins; --fetch or --adi-hdl/--corundum)
python3 scripts/assemble_workspace.py --build-root build/ws-a9 --fetch --profile adrv9361

# adrv9361 host side (docs/how-to/adrv9361-lablp.md): modules, offline plan, lablp session
scripts/build_lablp_modules.sh --linux-dir <real adi-linux checkout @ 2023_r2_p1> --work-dir build/lablp-modules
hopcap plan --plan examples/plans/adrv9361-loopback-8slot.json --dry-run
python3 examples/hardware/adrv9361_hopcap.py --inspect     # --run needs HOPCAP_OUT/_LABLP_BIN/_TICK_MODULE/_VRT49_MODULE

# libvrt49.so (needed by the chain test's golden codec)
cmake -S third_party/vrt49 -B build/vrt49 -DVRT49_BUILD_TOOLS=OFF -DVRT49_BUILD_TESTS=OFF && cmake --build build/vrt49 -j
```

## Invariants

- `hdl/hopctrl_regmap.json` is the single source for the controller register map. RTL localparams,
  `src/hopcap/hopctrl.py` constants and the docs table are all checked against it by tests.
- Offsets 0x00–0x14 and 0x40–0x7C are byte-identical in meaning to vrt49's `vrt49_ad9361_hop_ctrl`
  so its host tooling carries over. New registers start at 0x18.
- The controller never generates the hop. ZU11EG: Tick `gpio_out[0]` drives TAL_GPIO_0 directly
  (the validated path) and is also the controller's event; `gpio_out[3:1]` is the slot index.
  ADRV9361 (`adrv9361`): Tick `gpio_out[2:0]` drives AD9361 CTRL_IN[2:0] from IOB registers on the
  tick clock (FastLock profile k for RX and TX together) and `gpio_out[3]` is the event; the BD
  feeds the controller `{gpio_out[2:0], gpio_out[3]}` through `tick_gpio_sync`, so the controller
  still sees event = bit 0, slot = bits 3:1.
- With `enable=0` the controller forces the gate open, reproducing today's gapless vrt49 behaviour.
- Units: the controller's SETTLE / CAPTURE / DRAIN_TIMEOUT count its own clock, sys_cpu_clk at
  100 MHz (`hopctrl_regmap.json` `clock_hz`). On the ZU11EG default profile Tick also runs at
  100 MHz, so Tick ticks and controller cycles coincide (timed-dma: Tick at 122.88 MHz). On `adrv9361` Tick runs at **50 MHz** (it
  misses 100 MHz on the Z7035 -2L): Tick ticks = µs × 50, controller cycles = µs × 100. Plans are
  in µs; convert at each consumer, never mix them. `src/hopcap/platform.py` holds the per-board
  constants (Tick Hz, bit map, addresses, receiver); `HopController.program` takes the Tick rate
  and converts to controller cycles.
- Licences: new code MIT. Files derived from vrt49 keep their upstream SPDX header and are listed
  in `PROVENANCE.md`.

## Lab facts (verify in `~/dev/infra/HOSTS.md` before use)

Place `tron` (ADRV9009-ZU11EG). The cage is a build-time choice (`--port`, `VRT49_PHYSICAL_PORT`,
default `sfp`; receivers in `src/hopcap/link.py`): SFP+ ↔ nemo 82599 `enp2s0` (cabled since
2026-11-05), or QSFP lane 0 ↔ nemo XL710 `enp4s0f0np0` (that cage now carries mini2's breakout, so
`qsfp` needs tron's DAC moved back). nemo 192.0.2.2, FPGA 192.0.2.1:4991. Dispatch hw-test with the
`-slow` plan (the default): the 50 ms plan is below the staging floor on tron's serial control path.
Coordinator 10.0.0.41:20408. Shares `/tmp/vrt49-corundum-hardware.lock` with vrt49. ZU11EG: never
reprogram the PL at runtime (kills JESD); boot-resident images only.

ADRV9361-Z7035 (place `lablp`): the opposite rule. The AD9361 has no JESD link, the SD card is
never written, and the hopcap bitstream is loaded at runtime through the FPGA manager
(`system_top.bin` from `build_bitstream.sh`, byte-swapped by bootgen) after unbinding the AD9361
drivers, then the overlay is applied; a power cycle restores stock Kuiper.
