# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A composite example repo: Tick (hard-timed FHM GPIO hop) + vrt49 (VITA 49.2 over 10G QSFP) on the
ADRV9009-ZU11EG, joined by a new FPGA block `vrt49_fhm_hop_ctrl` that gates capture after a settle
delay and stamps the hop frequency + epoch into a hardware-emitted Context packet. Design document:
`docs/superpowers/specs/2026-10-23-hopcap-design.md` (read it before touching the architecture).

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
make -C hdl/sim/cocotb lint                       # Verilator --lint-only

# libvrt49.so (needed by the chain test's golden codec)
cmake -S third_party/vrt49 -B build/vrt49 -DVRT49_BUILD_TOOLS=OFF -DVRT49_BUILD_TESTS=OFF && cmake --build build/vrt49 -j
```

## Invariants

- `hdl/hopctrl_regmap.json` is the single source for the controller register map. RTL localparams,
  `src/hopcap/hopctrl.py` constants and the docs table are all checked against it by tests.
- Offsets 0x00–0x14 and 0x40–0x7C are byte-identical in meaning to vrt49's `vrt49_ad9361_hop_ctrl`
  so its host tooling carries over. New registers start at 0x18.
- The controller never generates the FHM pulse. Tick `gpio_out[0]` drives TAL_GPIO_0 directly (the
  validated path) and is also the controller's event; `gpio_out[3:1]` is the slot index.
- With `enable=0` the controller forces the gate open, reproducing today's gapless vrt49 behaviour.
- Capture length is in Tick ticks (100 MHz). Host converts from µs/samples.
- Licences: new code MIT. Files derived from vrt49 keep their upstream SPDX header and are listed
  in `PROVENANCE.md`.

## Lab facts (verify in `~/dev/infra/HOSTS.md` before use)

Place `tron` (ADRV9009-ZU11EG), QSFP lane 0 ↔ nemo XL710 `enp4s0f0np0` 192.0.2.2, FPGA 192.0.2.1:4991.
Coordinator 10.0.0.41:20408. Shares `/tmp/vrt49-corundum-hardware.lock` with vrt49. Never reprogram
the PL at runtime (kills JESD); boot-resident images only.
