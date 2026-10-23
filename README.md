# hopcap — hard-timed FHM hop, settled capture, hop frequency in VRT49 Context

**Status: in development. Offline pipeline complete (114 unit tests, 18 cocotb tests); first Vivado build in flight; nothing is hardware-validated yet. See `STATUS.md`.**

Composite example for the ADRV9009-ZU11EG (ADRV2CRR-FMC carrier). It assembles
[Tick](https://github.com/tfcollins/tick) and [vrt49](https://github.com/tfcollins/vrt49)
into one bitstream plus a host runner so that:

1. Tick fires the Talise A frequency-hopping-mode (FHM) GPIO on a deterministic
   schedule (the validated `gpio_out[0] -> TAL_GPIO_0` path).
2. A new FPGA block, `vrt49_fhm_hop_ctrl`, sees the same edge, closes sample
   admission, waits a configurable settle time, emits a VITA 49.2 Context packet
   carrying the hop RF frequency and a hop epoch, opens admission for a
   configurable capture length, then closes again.
3. IF Data and Context leave over the vrt49 10G UDP path on QSFP lane 0.
4. The host spools the stream, tags each capture with its Context, and writes
   SigMF plus an evidence bundle.

Settle time and capture length are single values for the whole plan.

## Layout

| Path | What |
|---|---|
| `third_party/tick`, `third_party/vrt49` | pinned submodules (`pins.json` is the contract) |
| `hdl/vrt49_fhm_hop_ctrl/` | hop controller RTL + ADI IP packaging |
| `hdl/hopctrl_regmap.json` | single source of truth for the controller register map |
| `hdl/system_bd_fabric_hop.tcl` | block-design fragment wiring Tick -> controller -> framer |
| `hdl/sim/cocotb/` | Verilator + cocotb testbenches |
| `scripts/` | workspace assembly, bitstream and boot-artifact builds, evidence verifier |
| `src/hopcap/` | host runner (`hopcap plan|run|decode|evidence|verify`) |
| `tests/unit`, `tests/hardware` | offline tests; labgrid-driven tests on place `tron` |

## Quick start (offline)

```sh
git submodule update --init
pip install -e third_party/tick -e ".[test]"
pytest -q                                                   # 114 unit tests
hopcap plan --plan examples/plans/band-2400-2500-4slot.json --dry-run
make -C hdl/sim/cocotb TOP=vrt49_fhm_hop_ctrl               # needs hdl/sim/.venv with cocotb 1.9
```

Build and lab flow: `docs/how-to/build.md`, `docs/how-to/tron-nemo-runbook.md`. Design document:
`docs/superpowers/specs/2026-10-23-hopcap-design.md`. `CLAUDE.md` has the command matrix and invariants.
