# hopcap — ZU11EG Tick FHM hop → settled, gated capture → VRT49 Context over QSFP

## Context

The DataX radar-tracking proposal (`datax_radar_tracking/docs/architecture.md` §6–7,
`timing-transport.md`) describes a dwell loop: close admission at T0, select the RF profile
with a hard edge, wait a qualified settling guard, publish Context epoch N+1, open capture at
T2, stream data tagged with that epoch. Nothing runnable demonstrates it. Tick has the hard
edge (validated Talise FHM hop on `tron`, 5/5, 0-tick skew), vrt49 has the 10G QSFP datapath
on the same board and a finished hop→Context pattern on a different board (ADRV9361). No
bitstream combines them, and Tick's own capture command is unwired on every ZU11EG build.

This plan creates a new composite repo, **`hopcap`** (`/home/tcollins/dev/radar_sdk/hopcap`,
→ `tfcollins/hopcap`), that assembles Tick + vrt49 into one ZU11EG QSFP bitstream with a new
FPGA hop controller, plus a host runner that produces per-hop captures tagged with the hop
frequency and an evidence bundle. It is the "the dwell loop is real" example the proposal can
point at.

### User decisions (2026-10-23)
- Capture gate = **FPGA hop controller** (port of vrt49's ADRV9361 pattern to Talise).
- Home = **new repo under radar_sdk**, consuming tick and vrt49.
- **One settle delay + one capture length** for the whole plan.
- Hardware base = vrt49 branch **`origin/test/zu11eg-qsfp-hw`** (merge to main first). Link is
  tron QSFP lane 0 ↔ nemo XL710 `enp4s0f0np0` 192.0.2.2, FPGA 192.0.2.1:4991.

### Planner's calls (not user-decided; override if wrong)
- Repo name `hopcap`; Python package/CLI `hopcap`; RTL block `vrt49_fhm_hop_ctrl`.
- **Capture length is in Tick ticks (100 MHz) in RTL**; host accepts `--capture-us` or
  `--capture-samples` and converts. Received samples are asserted within ±1 data packet
  (360 samples) of the expected count, because the framer finishes packets atomically.
- Tick `gpio_out[0]` keeps driving TAL_GPIO_0 exactly as validated **and** is the controller's
  event; `gpio_out[3:1]` = slot index (8 Q44.20 slot words, host-written). Controller never
  generates the FHM pulse. FHM one-behind pipeline is handled entirely host-side.
- Controller drops the ADRV9361 pending FIFO: on Talise the hop has already happened when the
  edge arrives, so "latest wins + superseded counter" is correct.
- Submodules (`third_party/tick`, `third_party/vrt49`) + `pins.json` + generated
  `PROVENANCE.md` hash table, not vendoring. ADI HDL pin = vrt49's `e57851ffc` (Corundum-
  validated base; Tick's `system_top` patch anchors verified present in `system_top_corundum.v`).
- Tick driver built **in-tree (`obj-y`)** in the vrt49 kernel build (ADRV9361 workflow precedent),
  so no vermagic step. `.ko` from the same tree only as a dev fallback.
- License: new code MIT (matches tick). Files derived from vrt49 keep their upstream SPDX
  (RTL BSD-2-Clause, C/Python LGPL-2.1-or-later) and are listed in `PROVENANCE.md`.
- Host NIC loss is **report-only** (known residual 1e-5..3e-4 `rx_missed_errors`); FPGA-side
  drops are hard failures.
- Deploy = SD staging (`VRT49_CORUNDUM_BOOT=sd`, how the QSFP run was done); JTAG supported.

## Architecture

```
host (nemo)                     tron PL (sys_cpu_clk 100 MHz)                 Talise A
hopcap run ──IIO──► axi_timed_command_scheduler_0 @0x86000000
   │ stage frequency_hopping_mode     │ gpio_out[0] ───────────────────────► TAL_GPIO_0 (FHM trigger)
   │ (one hop ahead, readback)        │ gpio_out[3:0] ─► vrt49_fhm_hop_ctrl_0 @0x84AC0000
   │ devmem: slots, SETTLE,                 IDLE→DRAIN→SETTLE→PUBLISH→OPEN(capture_ticks)→CLOSED
   │         CAPTURE_TICKS                  fabric_sample_enable / fabric_context_* ─► axi_vrt49_framer_0
   ◄── UDP 192.0.2.1:4991 ◄── Corundum QSFP lane0 ◄── udp_tx ◄── framer (IF Data + Context: CIF0 b27 RF freq, CIF2 b21 epoch)
```

Per hop k: Tick edge (slot k) → controller closes admission, drains, waits SETTLE, hands
slot-k frequency + epoch to the framer (Context emitted in hardware, before any data), opens
for CAPTURE_TICKS, closes. Host meanwhile stages frequency for hop k+1 and reads back the PLL.

### Address map (new rows only)
| PS addr | Block | IRQ |
|---|---|---|
| 0x86000000 | `axi_timed_command_scheduler_0` (Tick, BD 0x46000000) | ps-15 |
| 0x84AC0000 | `vrt49_fhm_hop_ctrl_0` | none |
(0x84A80000 framer, 0x84A90000 udp_tx, 0x84AA0000 rx_filter, 0x84AB0000 dac_tx, 0xA0000000 Corundum unchanged.)

## Repo layout

```
hopcap/
  README.md CLAUDE.md LICENSE PROVENANCE.md STATUS.md pins.json labgrid-plugins.lock pyproject.toml
  third_party/tick  third_party/vrt49                 # submodules
  hdl/vrt49_fhm_hop_ctrl/{vrt49_fhm_hop_ctrl.v, *_ip.tcl, Makefile}
  hdl/hopctrl_regmap.json                            # single source for RTL/host/docs register map
  hdl/system_bd_fabric_hop.tcl                       # composite BD fragment
  hdl/sim/cocotb/{Makefile, test_fhm_hop_ctrl.py, test_fhm_hop_chain.py, fhm_hop_chain.v}
  kernel/dts/zynqmp-adrv9009zu11eg-hopcap-overlay.dts
  overlays/patches/                                  # pre-upstream deltas vs staged tick/vrt49 (empty at release)
  scripts/{assemble_workspace.py, build_bitstream.sh, build_boot_artifacts.sh, check_hop_netlist.py,
           deploy_tron.py, run-tron-hopcap.sh}
  src/hopcap/{plan.py, descriptors.py, hopctrl.py, talise.py, scheduler.py, framer.py, transport.py,
              vrt.py, receiver.py, native/udp_spool.c, associate.py, export.py, evidence.py, run.py, cli.py,
              target/hopcap_target.py, lab/{boot.py, preflight.py}}
  tests/unit  tests/hardware  tests/fixtures  examples/hardware/zu11eg_hopcap.py  examples/plans/
  docs/ (Sphinx -W, MyST, adi_doctools cosmic, Diátaxis)   .github/workflows/{ci,sim,assemble-check,hdl-build,hw-test,docs}.yml
```

## Component specs

### 1. `vrt49_fhm_hop_ctrl` (copy of `vrt49/hdl/vrt49_ad9361_hop_ctrl/vrt49_ad9361_hop_ctrl.v`, keep AXI-Lite machinery, `apply_wstrb`, 8-slot table)
- Ports: as ADRV9361 block minus `fastlock_select`; add debug `gate_open`, `state[2:0]`.
- Event = rising edge of `scheduler_gpio[0]` while enabled. Slot = `gpio[3:1]` (SLOT_SOURCE=0) or an
  auto-incrementing sequence index (SLOT_SOURCE=1, enables Tick `REPEAT` rise/fall pairs).
- States: IDLE(gate=idle_open) → DRAIN(wait `fabric_samples_drained` or DRAIN_TIMEOUT→sticky) →
  SETTLE(settle_ticks) → PUBLISH(valid/ready, wait `fabric_context_accepted`) → OPEN(capture_ticks; 0 = until
  next event) → CLOSED. Event in DRAIN/SETTLE/PUBLISH: relatch latest slot, `superseded++`. `enabled=0` ⇒ gate
  forced open (today's gapless behaviour, so vrt49's existing ZU11EG hardware set still passes on the composite image).
- Registers (0x00–0x14, 0x40–0x7C identical to ADRV9361 block): 0x00 ID `HOP2`, 0x04 VERSION, 0x08 CTRL/STATUS
  (enable, slot_source, W1C clear, idle_open; RO state/gate/stickies), 0x0C SETTLE_TICKS, 0x10 GPIO_SEEN, 0x14 EPOCH,
  0x18 CAPTURE_TICKS, 0x1C SUPERSEDED_COUNT, 0x20 EVENT_COUNT, 0x24 CAPTURE_COUNT, 0x28 LAST_SLOT, 0x2C SEQ_SLOT_COUNT,
  0x30 SEQ_INDEX, 0x34 DRAIN_TIMEOUT_TICKS, 0x40–0x7C SLOT[0..7] Q44.20 (lo,hi). Published in `hdl/hopctrl_regmap.json`;
  a contract test asserts RTL localparams, `hopctrl.py` constants and the docs table all match it
  (pattern: `tick/tests/test_register_map_consistency.py`).
- cocotb (1.9.x + Verilator, Makefile mirrors `vrt49/hdl/sim/cocotb/Makefile`, reuse `axi_read/axi_write` from
  `test_udp_tx_axi.py` and `vrt49_codec.py` as golden decoder):
  `TOP=vrt49_fhm_hop_ctrl` unit suite (every state × event, W1C, byte-lane slot writes, capture_ticks exactness,
  enable=0 gate open); `TOP=fhm_hop_chain` = controller + `axi_vrt49_framer_axi` + `vrt49_pack`: assert no IF Data
  between event and Context, Context CIF0 b27 == slot word and CIF2 b21 == epoch, data packets during OPEN ==
  capture_ticks·rate/360 ±1, back-to-back events → exactly one Context per epoch. Stretch: add Tick RTL to the chain
  and drive it with a real `GPIO_WRITE` over AXI-Lite.

### 2. Block design + top-level (`hdl/system_bd_fabric_hop.tcl`)
Sourced after `system_bd_vrt49.tcl` and after Tick's unmodified
`tick/hdl/projects/adrv9009zu11eg/adrv2crr_fmc/tick_adrv9009zu11eg_adrv2crr_fmc.tcl`. Deletes the four framer
fabric tie-off nets (same net-deletion pattern `system_bd_vrt49.tcl:634-639` uses for `axi_tx_fifo`) until the
vrt49 `VRT_FABRIC_HOP_ENABLE` hook lands; instantiates the controller on `sys_cpu_clk`; connects
`tick_fhm_gpio_slice/Dout` → `scheduler_gpio` (same net also leaves as the `tick_fhm_gpio` port → TAL_GPIO_0);
wires the seven fabric nets exactly as `adrv9361z7035_vrt49_sfp/system_bd.tcl:116-132`; `ad_cpu_interconnect 0x84AC0000`.
Top-level: Tick's existing `patch_system_top()` from `tick/scripts/prepare_adi_hdl_adrv9009.py` applied to the staged
`system_top_corundum.v` (anchors verified identical at e57851ffc). No new XDC.

### 3. `scripts/assemble_workspace.py` (codifies `vrt49/.github/workflows/hdl-corundum.yml` L283-345 + Tick)
Fail-closed on submodule SHAs ≠ `pins.json`; fetch/validate ADI hdl `e57851ffc` + corundum `37f2607` (+ vrt49's
corundum patch); rsync vrt49's IP list + ZU11EG project into the tree; run vrt49's `configure_dual_talise_links.py`
and `check-zu11eg-qsfp-contract.py`; stage `hdl/vrt49_fhm_hop_ctrl` → `library/`; copy Tick RTL + IP tcl →
`library/axi_timed_command_scheduler/`, Tick fragment → `projects/adrv9009zu11eg/adrv2crr_fmc/`; `patch_system_top()`;
anchored idempotent edits to the staged project (`system_project.tcl` adds Tick `.v` to the file list,
`system_bd.tcl` sources Tick fragment + `system_bd_fabric_hop.tcl`, `Makefile` adds `LIB_DEPS`/`M_DEPS`);
apply `overlays/patches/*`; write `MANIFEST.json` (SHA-256 of every staged file) → `PROVENANCE.md`.
`build_bitstream.sh`: `make QSFP_ENABLE=1 SFP_REFCLK_MHZ=156.25 VRT_*` (CI tuple), then vrt49's
`check-vivado-timing.py`, `check-sfp-rx-netlist.py`, new `check_hop_netlist.py` (controller instance, `tick_fhm_gpio`,
`adrv9009_gpio_00_a = tick_fhm_gpio[0]`); local or `ssh hdl-dev-2` (pattern `tick/scripts/build_adi_hdl_tick_remote.sh`).

### 4. `scripts/build_boot_artifacts.sh` (codifies the QSFP-branch `kernel-control` job + BOOT.BIN step)
BOOT.BIN via `vrt49/scripts/build-xilinx-boot-bin.py --platform adrv9009-zu11eg-qsfp`. Kernel: ADI linux `2e89089`,
vrt49's patches + `iio-vrt49-buffer.c` obj-y, **plus** Tick driver/UAPI copied in as obj-y (exactly
`hdl-adrv9361-sfp.yml` L139-141). DTB: QSFP base + `patch_ad9545_sfp_clock.py --output q1b` + vrt49 integrate steps,
then `fdtoverlay` vrt49's framer-control overlay + `kernel/dts/zynqmp-adrv9009zu11eg-hopcap-overlay.dts`
(Tick node `adi,axi-timed-command-scheduler-1.0` @0x86000000 clocks `zynqmp_clk 71`, `adi,time-tick-hz=100000000`;
hop node `nous,vrt49-fhm-hop-controller-1.0` @0x84ac0000, no driver). fdtget assertions: Q1B `output-clk@8`, rates,
framer/rx-filter nodes, Tick + hop nodes. No adrv9009 node change (driver defaults fhmGpioPin=0 match).
`scripts/verify_evidence.py` modeled on `tick/scripts/verify_tick_evidence.py`.

### 5. Host package `src/hopcap/`
- `plan.py`: `HopPlan(frequencies_hz, period_ticks, settle_ticks, capture_ticks, pulse_ticks=10 ms, lead_ticks=50 ms,
  init_frequency_hz=2.45 GHz, band, fs=245.76 MSPS)`; `SlotTable` (≤8 unique incl. init); `Edge` k commits
  `frequencies[k-1]` (edge 0 = priming edge → fhmInit, flagged in manifest) and stages `frequencies[k]` afterwards;
  `FhmPipelineModel` (LATE_DROPPED edge leaves staged unchanged); `compile_plan()` fails closed on
  `period ≤ settle + capture + pulse + margin`, out-of-band, >8 slots, period below host-staging floor (doc: "hard-timed
  edge, soft-timed staging").
- `descriptors.py`: two `tick.CommandDescriptor` GPIO_WRITE per edge — rise `args=(0xF, slot<<1|1, 0, 0)`
  `REPORT_COMPLETION|LATE_DROP`; fall `args=(0x1, 0, 0, 0)` `LATE_DROP`, no completion. CSV via `tick.ffh.descriptor_csv`.
  Unit test replays through `tick.hdl_model.AxiTimedCommandSchedulerModel`.
- `hopctrl.py`: `HopCtrlRegs.from_json(hdl/hopctrl_regmap.json)`; `program(settle, capture, slots)`, `require_idle()`,
  `wait_drained()` (from `vrt49/scripts/adrv9361_tick_hop.py`). MMIO via on-target `target/hopcap_target.py`
  (stdlib `/dev/mem` mmap, one JSON batch per call, uploaded once via vrt49's base64 upload helper); `devmem` fallback.
- `talise.py`: `FhmStager` over pylibiio or `iio_attr` (LO channel found by `frequency_hopping_mode_enable`, as in
  `tick/examples/adrv9009_fhm_hop_demo.py`); `park_trigger_low()`.
- `scheduler.py`: `TickSession` on `tick.ffh.iio_device_io`/`wait_for_completion`; lookahead 4 hops (≤ depth/2) with refill;
  after each rise completion: `pipeline.on_edge`, `stage(next)`, `stage_late` detection vs `current_time_ticks`, PLL readback.
- `vrt.py`: pure-Python VITA 49.2 parser/builder (prologue + CIF walker mirroring `vrt49/include/vrt49/cif.h`; Q44.20;
  CI16 payload → `int16 (N,2)`; accepts short tail packets). Cross-checked in CI against `vrt49_codec.py` with `libvrt49.so`
  built from the submodule.
- `receiver.py` + `native/udp_spool.c`: derived from PR #52 `sfp_udp_capture.c` (recvmmsg, 64 MB SO_RCVBUF, `ready`/SIGUSR1/
  SIGTERM protocol) but spools every datagram with arrival mono/real ns; `--max-bytes` fail-closed; `taskset` after spawn
  (not `preexec_fn`). `PyReceiver` writes the identical format for tests. `host_rx_counters(enp4s0f0np0)`.
- `associate.py`: segment IF Data into windows by timestamp gap; assign nearest preceding Context with next expected epoch;
  never guess (lost Context ⇒ hop `invalid`); cross-check `context.rf_hz == readback == commits_hz`; samples vs expected ±360.
- `export.py`: SigMF (`run.sigmf-data` ci16_le + `captures[]` per hop with `core:frequency`), `hops/hop_NNNN.npy`,
  `manifest.json` (`hopcap-manifest/v1`: plan, provenance SHAs, clock domains with explicit "Tick time and VRT time not
  mapped", counters, per-hop records, disposition). Raw spool retained for replay (`hopcap decode`).
- `evidence.py`: bundle (`summary.txt`, `SHA256SUMS`, `evidence.json` in datax `datax-radar-evidence/v1` shape, JUnit, labgrid
  logs) + `verify()` (clone of `verify_tick_evidence.py::verify_tron_repeat`, fixture-tested).
- `cli.py`: `hopcap plan --dry-run` (JSON: slots, edges, 12-word descriptor hex, timing checks; offline), `run`, `decode`,
  `evidence`, `verify`.

### 6. Hardware harness (`tests/hardware/`, `lab/`, launcher)
`lab/boot.py` lifts the pure logic of vrt49's `corundum_shell` (QSFP branch `conftest.py` L90-229: SD exact-artifact staging,
sha verify, power-cycle, restore) — copy-adapted, hashed in `PROVENANCE.md`; keeps vrt49 env names (`LG_ENV`,
`VRT49_CORUNDUM_BOOT=sd`, `VRT49_PHYSICAL_PORT=qsfp`, `VRT49_CORUNDUM_*_URL/SHA256`, `VRT49_SFP_*`) + `HOPCAP_*`.
`lab/preflight.py`: nemo XL710 up at 192.0.2.2, DAC link, i40e tuning state (record only), lock free, coordinator, place tags.
`examples/hardware/zu11eg_hopcap.py`: same conventions as `vrt49/examples/hardware/zu11eg.py` (`--inspect/--dry-run`,
`--run` wraps `adi-lg request --part adrv9009zu11eg --carrier adrv2crr-fmc --mode reserve`, **shares**
`/tmp/vrt49-corundum-hardware.lock`, JUnit validation, refuses preset `LG_*`).
Tests: `test_boot_and_identity`, `test_hop_capture_plan`, `test_negative_no_edge_no_hop`, opt-in report-only `test_settle_sweep`.

| Check | Disposition |
|---|---|
| Boot artifact SHAs live; devices present; `time_tick_hz`=1e8; capability GPIO; hop ctrl ID `HOP2` | hard |
| Every rise completion EXECUTED, `executed == requested`; `late_count`/`overflow_count` Δ=0 | hard |
| Talise `frequency_hopping_mode` readback == pipeline expectation (+0 Hz observed; tol 2 MHz) | hard |
| Per hop: `context.rf_hz == readback == commits_hz`; epoch strictly +1; Context before its data | hard |
| Samples per complete hop == capture_ticks·fs/1e8 ±360; timestamps monotonic, mod-16 continuity | hard |
| Framer `v49_tx_dropped_packets`/udp drop/length-error Δ=0; malformed 0; controller drained, superseded 0 | hard |
| ≥95 % of non-priming hops complete (`HOPCAP_MIN_COMPLETE_FRACTION`) | hard |
| NIC `rx_missed_errors`, per-hop loss, hops invalid from lost Context, `stage_late`, settle-quality metric | report |

### 7. CI, docs, siblings
- `ci.yml` (hosted): submodules, `pip install -e third_party/tick -e .[test]`, ruff 0.16.1, build `libvrt49` for codec
  cross-check, `pytest tests/unit`, `pytest tests/hardware --collect-only`, `cc -Wall -Wextra -Werror udp_spool.c`.
- `sim.yml`: Verilator lint + cocotb suites. `assemble-check.yml`: run the assembler against shallow pinned fetches, diff vs
  manifest (no Vivado). `hdl-build.yml`: manual, self-hosted `vivado`. `hw-test.yml`: `workflow_dispatch` + PR label
  `hw-test` + nightly offset from vrt49; runner `hw-nemo`; evidence bundle artifact (Garage fallback).
- Docs: Diátaxis — dry-run tutorial, first-hardware-run, tron/nemo runbook (SD stock-file caveat, `LG_FORCE_LOCAL_XSDB=1`,
  never upgrade nemo `venv_WORKING`), architecture (datax T0/T1/settle/N+1/T2 mapped onto edge/SETTLE/Context/OPEN),
  fhm-pipeline, loss-accounting, CLI/manifest/regmap references (regmap generated from JSON), requirements traceability,
  dated reports.
- `datax_radar_tracking`: `docs/examples/zu11eg-fhm-hop-capture.md` (proposed register; maps RAD-DATA-001/006/007/008,
  RAD-TIME-001/002/007/008/009, RAD-ARCH-005/007, RAD-API-007; honest limits) + toctree + one line in `verification.md`.
  Run its four CI steps locally. No uppercase VALIDATED.
- `~/dev/infra/HOSTS.md`: add tron QSFP lane0 ↔ nemo XL710 `enp4s0f0np0` (MAC 64:9d:99:6f:a9:6a, 192.0.2.2/24, MC2210128-003
  DAC, refclk AD9545 Q1B 156.25 MHz, residual loss note, shared lock, tron 10.0.0.79 as of 2026-10-22, SD or JTAG boot).

### 8. Upstream changes (tracked as PRs; composite carries `overlays/patches/` until merged)
- vrt49 A (hard): merge `test/zu11eg-qsfp-hw` → main. B–E (soft): `VRT_FABRIC_HOP_ENABLE` param in `system_project.tcl`,
  guard around tie-offs in `system_bd_vrt49.tcl:620-629`, Makefile var, keep `test_adrv9361_sfp_contract.py:191` green.
  F (later): upstream `vrt49_fhm_hop_ctrl` into `vrt49/hdl/` + `hdl-sim.yml` + IP doc page.
- tick G (soft): `prepare_adi_hdl_adrv9009.py --project-dir/--system-top` so it targets `system_top_corundum.v` natively.
  Also bump vrt49's `TICK_PROVENANCE.md` pin if vrt49 ever consumes REPEAT.

## Phases (each independently verifiable)

| # | Deliverable | Verified by |
|---|---|---|
| 0 | Repo skeleton, submodules, `pins.json`, CLAUDE.md, CI skeletons, docs `-W` | `git submodule status` == pins; empty suites green |
| 1 | `vrt49_fhm_hop_ctrl.v` + regmap JSON + cocotb unit suite + lint | cocotb `TOP=vrt49_fhm_hop_ctrl` |
| 2 | `fhm_hop_chain` cocotb (controller+framer, golden codec); stretch with Tick RTL | Context-before-data, fields, capture length |
| 3 | Offline host core: `plan/descriptors/hopctrl/vrt/associate/export/cli plan+decode` | unit tests, golden dry-run JSON, hdl_model replay, synthetic streams |
| 4 | Receiver (`udp_spool.c`, `PyReceiver`), software loopback | byte-identical spools; full decode of synthetic run |
| 5 | Target/control: `hopcap_target.py`, `talise.py`, `scheduler.py`, `framer.py`, `run.py` | FakeTick/FakeMmio tests; `--dry-run` through `run.py` |
| 5b | **Early hardware de-risk** (no composite image): staging loop vs Tick's existing `--fhm-pins` bitstream on tron; `CReceiver` vs vrt49 QSFP image | proves soft-timed staging margin and 8.4 Gb/s spool independently |
| 6 | `assemble_workspace.py`, `system_bd_fabric_hop.tcl`, patch queue, assembler tests, `assemble-check.yml` | staged tree diff == manifest |
| 7 | Vivado build on hdl-dev-2; timing/netlist checks; XSA/psu_init | `check-vivado-timing.py`, `check_hop_netlist.py` |
| 8 | `build_boot_artifacts.sh`, overlay DTS, `verify_evidence.py` | fdtget assertions; SHA manifest |
| 9 | Harness (`lab/`, `tests/hardware`, launcher, evidence) → first composite run on tron | table above; vrt49's existing ZU11EG set still passes with controller disabled |
| 10 | `hw-test.yml`, docs, dated report, datax page, HOSTS.md, upstream PRs, drop patch queue, re-pin | label-gated run green; datax CI green; `overlays/patches/` empty |

## Verification (end-to-end)
1. `nox`/`pytest tests/unit -q` and `make -C hdl/sim/cocotb TOP=fhm_hop_chain` green on a dev box (no Vivado/hardware).
2. `hopcap plan --plan examples/plans/band-2400-2500-8slot.json --dry-run` matches golden JSON.
3. `scripts/assemble_workspace.py && scripts/build_bitstream.sh --remote hdl-dev-2`; `scripts/build_boot_artifacts.sh`; `scripts/verify_evidence.py build`.
4. `examples/hardware/zu11eg_hopcap.py --run --junit out/junit.xml` on nemo (reserves `tron`, SD-stages artifacts, runs
   `tests/hardware`, writes bundle); `hopcap verify out/evidence/<run>` passes; manifest shows every non-priming hop with
   Context RF freq == Talise readback, epoch monotonic, capture length within ±1 packet, FPGA drops 0.
5. Re-run vrt49's existing ZU11EG Corundum hardware set against the composite image with the controller disabled → 9/9.

## Key risks
- Settle value unknown (no Talise FHM settle figure anywhere); make it a knob, sweep report-only, note packet-atomic tail
  (~1.5 µs) + FIFO latency in docs.
- Hop rate is bounded by per-hop software staging of `frequency_hopping_mode` (ms-class), not Tick; missed staging ⇒
  mislabeled hop — readback check catches it; document "hard-timed edge, soft-timed staging".
- `fabric_samples_drained` needs momentary input idle — fine at 245.76 MSPS (61.44 Mbeat/s < 100 M/s) but add DRAIN_TIMEOUT.
- One Context per hop at 1e-4 NIC loss ⇒ ~1 untaggable hop per 10k; marked invalid, never guessed. Optional HDL follow-up:
  repeat Context (refresh) after the window.
- Init frequency 2450 MHz occupies a slot ⇒ max 7 other unique frequencies; documented.
- ADI HDL pin is `e57851ffc` (vrt49) while Tick validated on `hdl_2026_r1`; anchors match but first Vivado build is a risk;
  IOB path for `adrv9009_gpio_00_a` is new to this project.
- Never reprogram PL at runtime (kills JESD/RX DMA); boot-resident images only. SD stock-file restore caveat from the
  QSFP report — keep an independent stock copy.
- Tick queue depth 16 ⇒ 8 hops per submission; refill + optional sequence-slot/REPEAT mode.
- Lab coupling: shared lock with vrt49 on nemo, nightly offset, pinned labgrid-plugins, GitHub artifact quota (Garage), tron IP drift.

## Critical files to reuse
- `tick/examples/adrv9009_fhm_hop_demo.py`, `tick/src/tick/ffh.py`, `tick/src/tick/hdl_model.py`, `tick/scripts/prepare_adi_hdl_adrv9009.py` (`patch_system_top`), `tick/hdl/projects/adrv9009zu11eg/adrv2crr_fmc/tick_adrv9009zu11eg_adrv2crr_fmc.tcl`, `tick/scripts/verify_tick_evidence.py`
- `vrt49/hdl/vrt49_ad9361_hop_ctrl/vrt49_ad9361_hop_ctrl.v`, `vrt49/hdl/projects/adrv9361z7035_vrt49_sfp/system_bd.tcl:104-135`, `vrt49/hdl/projects/adrv9009zu11eg_vrt49_corundum/system_bd_vrt49.tcl:595-640`, `vrt49/hdl/axi_vrt49_framer/axi_vrt49_framer.v` (fabric ports L131-141, L517-524, L744-782, L907-928), `vrt49/hdl/sim/cocotb/{Makefile,vrt49_codec.py,test_udp_tx_axi.py,test_framer.py}`
- `vrt49/.github/workflows/hdl-corundum.yml` L283-345 (staging), L395-490 (Vivado/BOOT.BIN), L614-735 (kernel/DTB); `hdl-adrv9361-sfp.yml` L139-141 (Tick obj-y)
- QSFP branch: `tests/integration/conftest.py` L90-229, `test_hw_corundum_sfp.py` (`parse_vita_datagram` L199, framer config), `kernel/iio-vrt49-buffer/patch_ad9545_sfp_clock.py`, `docs/reports/2026-10-21-zu11eg-qsfp-hardware.md`; PR #52 `sfp_udp_capture.c`
- `vrt49/scripts/adrv9361_tick_hop.py`, `vrt49/examples/hardware/zu11eg.py`, `vrt49/tests/unit/test_zu11eg_examples.py`
- `datax_radar_tracking/docs/{architecture.md,timing-transport.md,verification.md,index.rst}`, `~/dev/infra/HOSTS.md`
