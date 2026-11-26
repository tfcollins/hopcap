# Run on the ADRV9361-Z7035 (lablp)

The `adrv9361` platform hops RX **and** TX together through AD9361 FastLock pin select: Tick
`gpio_out[2:0]` drives CTRL_IN[3:1], so one 3-bit code selects RX profile k and TX profile k on a
Tick tick. Design: `docs/superpowers/specs/2026-11-25-adrv9361z7035-hop-design.md`. This page is
the host side: artifacts, the board session on labgrid place `lablp`, and what each step checks.
Verify lab facts against `~/dev/infra/HOSTS.md` before use.

## What runs where

| Piece | Where | Notes |
|---|---|---|
| Board | ADRV9361-Z7035 on ADRV1CRR-FMC, place `lablp` | boots its stock Kuiper 2023_R2_P1 SD card; the SD card is never written |
| Bitstream | `system_top.bin` from `scripts/build_bitstream.sh` (profile `adrv9361`) | byte-swapped for the Zynq-7000 FPGA manager, loaded at runtime |
| Overlay | `kernel/dts/zynq-adrv9361z7035-hopcap-overlay.dts` | compiled with `dtc -@` by the session |
| Modules | `axi_timed_command_scheduler.ko`, `iio-vrt49-buffer.ko` | `scripts/build_lablp_modules.sh` (ARMv7, `6.1.70-284114-g2e8908932dfd`) |
| Receiver | nemo `enp7s0` (e1000e, `68:05:ca:19:88:8c`, `192.168.2.10/24`) | the 1G SFP is cabled there; the framer sends from `192.168.2.211` |

## 1. Build the modules

Needs Docker and a real ADI Linux checkout at `2023_r2_p1` (`2e8908932dfd`); a git worktree is
refused, because the kernel's version suffix and the commit check come from its git metadata.
hopcap itself may be a worktree: the script copies the module sources and Tick's
`prepare_adi_linux_kernel_tree.py` into the work directory and mounts only that copy.

```sh
scripts/build_lablp_modules.sh --linux-dir /path/to/adi-linux-2023_r2_p1 --work-dir build/lablp-modules
# -> build/lablp-modules/axi_timed_command_scheduler.ko, build/lablp-modules/iio-vrt49-buffer.ko
```

The defaults target lablp: `--config third_party/tick/ci/lablp/kuiper-2023_r2_p1-zynq-adrv9361-z7035.config.gz`,
`--localversion -284114`, `--expect-kernelrelease 6.1.70-284114-g2e8908932dfd`. The build fails
unless both modules carry that vermagic. Add `--dry-run` to print the container command.

## 2. Check a plan offline

```sh
hopcap plan --plan examples/plans/adrv9361-loopback-8slot.json --dry-run
```

A plan names up to eight `(rx_lo, tx_lo)` slots and the slot of every hop, in microseconds.
Profile 0 is live before hop 0. Over the TX1 to RX1 coupling, a TX DDS tone at `f_dds` lands in RX
baseband at `tx_lo - rx_lo + f_dds`. `FastLockPlan.validate()` rejects:

- more than eight slots, or two slots with the same pair;
- a tone offset outside ±0.4 fs, or within `dc_guard_hz` of DC;
- consecutive hops whose offsets coincide (closer than `min_step_hz`), or where the new offset
  sits on the previous tone's image;
- a period under 1 s. Each rising edge's Tick completion must be acknowledged over network IIO
  before the next edge (Tick's completion window holds one entry, and LATE_DROP drops a held
  command). On lablp, network IIO calls stall in steps of about 1 s.

The two example plans run at 15.36 MSPS with LOs near 2.4 GHz and a 1.25 s period. The period is
not a whole second, so the hops sample several phases of the PPS second. Both plans include
TX-only, RX-only and both-LO hops.

Units: Tick ticks = µs × 50 (Tick runs at 50 MHz); hop controller SETTLE / CAPTURE /
DRAIN_TIMEOUT = µs × 100 (its own 100 MHz `sys_cpu_clk`). The dry run prints both. A hop to slot k
is a rise `mask 0xF value 0x8|k` and, one pulse later, a fall `mask 0x8 value 0`.

## 3. Run a session

```sh
export HOPCAP_OUT=$PWD/evidence/lablp-$(date -u +%Y%m%dT%H%M%SZ)
export HOPCAP_LABLP_BIN=evidence/bitstream-a9-<stamp>/system_top.bin
export HOPCAP_TICK_MODULE=build/lablp-modules/axi_timed_command_scheduler.ko
export HOPCAP_VRT49_MODULE=build/lablp-modules/iio-vrt49-buffer.ko
export HOPCAP_PLAN=examples/plans/adrv9361-loopback-8slot.json   # optional (this is the default)
# HOPCAP_LABLP_NO_RUN=1 stops after bring-up; HOPCAP_TARGET_PASSWORD overrides Kuiper's "analog"
python3 examples/hardware/adrv9361_hopcap.py --inspect   # offline: the exact adi-lg argv
python3 examples/hardware/adrv9361_hopcap.py --run
```

`--run` does the following:

1. Checks nemo's `enp7s0` (by MAC and driver). Its `192.168.2.10/24` address is only reported
   here, because the NIC has carrier only while lablp is powered; the session's `host_receiver`
   stage requires it once the SFP link is up. Artifact paths are made absolute.
2. Records the place header. The rest of the listing is never kept, because it carries the power
   outlet's token.
3. Runs `adi-lg request --part adrv9361z7035 --carrier adrv1crr-fmc --mode uri --power-down --run …`.
   Its output is filtered for tokens.
4. Requires `lablp` released afterwards, and the run bundle to pass `verify_evidence.py run`.

It takes no host lock: labgrid's reservation serialises `lablp`, and `enp7s0` serves only this board.

Inside the reservation, `python -m hopcap.lab.lablp --evidence $HOPCAP_OUT` runs these stages
against `IIO_URI`. Each stage's result, or its error, is written to `session.json`.

| Stage | Does | Fails when |
|---|---|---|
| `connect` | root ssh (Kuiper's password-only login, one askpass-authenticated master, run-local `known_hosts`) | kernel is not `6.1.70-284114-g2e8908932dfd`, or a module's vermagic differs |
| `push` | `.bin`, `.dtbo`, both modules, the load script, each sha256-checked on the board | a hash differs |
| `load` | `scripts/lablp_hopcap_runtime_load.sh`: **AD9517 OUT4 = 125 MHz and enabled first**, then the same unbind / FPGA manager / rebind order as Tick's validated lablp load, the overlay, both modules, `iiod` restart | OUT4 does not read back `125000000`/`1`, FPGA manager not `operating`, overlay not `applied`, a device missing, the loaded `.bin` is not the pushed one |
| `devices` | network IIO lists Tick, `vrt49_framer`, `ad9361-phy`, `cf-ad9361-lpc`, `cf-ad9361-dds-core-lpc` | a device is missing, or `time_tick_hz` is not 50000000 |
| `radio` | 15.36 MSPS, RF bandwidth, manual RX gain, TX attenuation, TX1 DDS tone at `f_dds` (1 MHz) | an attribute is missing or the rate reads back wrong |
| `rx_buffer` | `in_voltage0..3` and the `cf-ad9361-lpc` buffer on | not exactly those four channels enabled |
| `link` | framer UDP endpoint = `enp7s0` (applied, read back), PCS status at framer `0x78` | `resetdone` (bit 16) is 0, or the module is absent |
| `host_receiver` | `192.168.2.10/24` on `enp7s0` on this host, now that the SFP has carrier | the address does not appear within ~30 s |
| `timebase` | time base on PPS source `tick`, LOCKED; measured beats per PPS; Tick epoch aligned (below) | not locked, period outside the margin, or the alignment check fails |
| `run` | `run_live` with the plan; bundle in `$HOPCAP_OUT/run` | the run's disposition is not `ok` |

`adi-lg --power-down` powers the board off afterwards. That returns it to stock Kuiper.

### Why all four RX channels

The VRT tap and the time base count beats of `util_ad9361_adc_pack/packed_fifo_wr_en`.
util_cpack2 packs only the enabled channels. With all four enabled (RX1 and RX2 I/Q), a beat is one
sample instant, so beats arrive at fs and the overlay's `adi,nominal-sps = 15360000` holds. With
one I/Q pair, each beat carries two samples: the beat rate halves to 7.68 MHz, and the tap forwards
every other RX1 sample. The `timebase` stage reads `TB_PPS_PERIOD` and reports that case by name.
Nobody reads the buffer: the DMA stalls once its blocks fill, but the packer keeps firing.

## 4. FastLock bank

`hopcap.fastlock.FastLockBank` loads the profiles in this order:

1. Turn the driver's pin control off (`adi,{rx,tx}-fastlock-pincontrol-enable` = 0).
2. For each slot k: set the RX LO, `fastlock_store k` on `altvoltage0`; set the TX LO,
   `fastlock_store k` on `altvoltage1`.
3. `fastlock_recall 0` on both.
4. Check that `0x25A` and `0x29A` read mode enable, profile 0, pin select off.

`run_live` then has Tick park all four GPIO bits low (CTRL_IN code 0 = profile 0). Only after that
does it set Profile Pin Select, with a board-side read-modify-write of D1|D0 in both registers
through the PHY's debugfs `direct_reg_access`, read back. Teardown clears D1 again.

Two rules hold once pin select is on:

- No LO write and no recall: the driver rewrites the setup registers on either.
- The driver's cached `frequency` readback is never taken as evidence of a hop. It reports what
  the host last wrote, not what a pin recall selected, so each edge's `readback_hz` is null.

## 5. VRT time = Tick time

`tick_pps_gen` on the Tick clock drives both Tick's `pps_in` and the time base's PPS source
`"tick"`. Its period is exactly 50,000,000 Tick ticks.

`hopcap.timebase.TickAligner` aligns the two clocks in one board-side shell command, so no network
round trip sits inside the window:

1. Require the time base LOCKED on `tick` and Tick's queue empty.
2. Wait for `v49_tb_seconds` to change. An edge has just passed.
3. Write Tick `time_epoch_ticks = N × 50e6`, Tick `arm_time_next_pps = 1` and framer
   `v49_tb_seconds = N`, back to back.
4. After the next edge, read framer seconds, Tick `current_time_ticks`, framer seconds again.
   When the two seconds reads agree, require `ticks // 50e6 == seconds` and `time_aligned == 1`.

**Race window:** the three writes must land between the same two edges. They take microseconds.
The window is lost only if the shell is preempted for the rest of the second, and then step 4
sees the two counters a second apart and fails. The two PPS consumers synchronise the pulse with
different latencies (Tick's flops on the Tick clock, the time base's synchroniser on the AD9361
data clock). That offset is constant per image and lands in the fixed hop latency.

**Sub-second time:** the PPS comes from the PS crystal, the sample beats from the AD9361's own
reference. The framer stamps the fraction as `floor(k × q / 2^32)` ps after the edge, where `k`
is the beat count since the edge and `q` the nominal `ps_per_sample_q32`. At a 40 ppm offset,
reading `k` at the nominal rate is 40 µs off by the end of the second.
`hopcap.timebase.vrt_to_tick_ticks(sec, frac_ps, tick_hz=…, ps_per_sample_q32=…, beats_per_pps=…)`
therefore:

- inverts the fraction exactly to `k`;
- returns `sec × 50e6 + k × 50e6 / beats_per_pps`, using the **measured** `TB_PPS_PERIOD`
  (framer register `0x60`, MMIO only).

The run records that register before and after the hops (`time_base.pps_period_before/after` in
the manifest).

## 6. A run by hand

Inside a reservation (for example `adi-lg request … --mode uri --run "$SHELL"`), after a
`HOPCAP_LABLP_NO_RUN=1` session has brought the board up:

```sh
hopcap run --plan examples/plans/adrv9361-loopback-3slot.json \
  --iio-uri "$IIO_URI" --target "root@${IIO_URI#ip:}" --out evidence/lablp-manual
```

`--platform adrv9361` is implied by the plan's `platform` key. The receiver binds
`192.168.2.10:4991` (nemo `enp7s0`), the framer sends from `192.168.2.211`, and ssh uses
`HOPCAP_TARGET_PASSWORD` (default `analog`).

## Troubleshooting

| Symptom | Cause |
|---|---|
| `resetdone (framer 0x78[16]) is 0` | The SFP reference: AD9517 OUT4 must be 125 MHz and enabled before the PL load (`runtime-load.log`). |
| `measured 7680000 beats per PPS` | Only one RX I/Q pair is enabled (see above). |
| time base never LOCKED in strict mode | No beats at all (buffer off), or PPS source not `tick`. |
| `Tick time … but framer second …` | The alignment straddled an edge. Rerun the session. |
| module refused by the kernel | vermagic: rebuild with `build_lablp_modules.sh` against the 2023_r2_p1 checkout. |
