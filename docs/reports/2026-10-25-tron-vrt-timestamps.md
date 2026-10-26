# Real VRT timestamps on tron — 2026-10-25

**Result:** 4/4 hardware tests pass, 5/5 hops `ok`, and every IF Data packet now carries a
sample-exact VRT timestamp from the framer's PPS time base. Before this, every timestamp hopcap
received was 0 (run 7, and vrt49's Phase 0 DPDK runs on the same image).

**Image:** bitstream attempt 3 (`e77d095f…`) and the boot set built from its XSA (Image
`11478997…`, system.dtb `100d7034…`), booted over JTAG. **Plan:** `band-2400-2500-4slot-slow`
(200 ms period, 200 µs settle, 1 ms capture), 5 edges. **Host:** nemo, kernel receiver
(`udp_spool`), XL710 ring 8160. **hopcap:** `147bc66`.

## Cause

The framer stamps packets from its PPS time base only while `v49_tb_mode` is enabled. vrt49's
ZU11EG overlay boots it disabled (`adi,timebase-start-disabled`), so timestamping stays opt-in,
and hopcap never enabled it. With it disabled the framer stamps every packet from its legacy TS
registers, which are 0. On this run the state found at connect confirmed it: `v49_tb_mode` 0
while the time base itself was already LOCKED to Corundum's PPS and counting (seconds 85).

## Fix

`framer.configure()` arms the time base while the framer is still disabled, in the order vrt49's
Corundum PPS hardware test uses (`ee31887`):

1. `v49_tb_mode = 0`: the PPS source cannot change while timestamps are enabled (`EBUSY`).
2. `v49_tb_pps_source = corundum`, then clear the resync sticky bit.
3. `v49_tb_seconds = ` the next host UTC second; it loads at the next PPS edge.
4. `v49_tb_mode = 3` (enable + strict: no data while unlocked).
5. Wait for the seconds counter to move. Its first new value must be the armed second, and the
   time base must be LOCKED, or the run stops before the radio is touched.

`hopcap.stamps` then checks every step between consecutive IF Data packets of a capture against
the sample count: `floor(360 × q / 2^32)` ps or 1 ps more, with `q` = `v49_tb_ps_per_sample_q32`.
The manifest records the time base before arming, after arming and after the run (`time_base`,
see the [manifest schema](../reference/manifest-schema.md)). The hardware test fails on zero
stamps, step errors, lost lock, a synthetic PPS edge or a resync.

## Results

| Check | Result |
|---|---|
| Arm | second 1792978908 (2026-10-26T01:41:48Z) loaded at the next edge; mode 3, LOCKED, source `corundum` |
| After the run | LOCKED, 0 missed edges, resync sticky clear |
| Timestamps | 3,415 IF Data packets, 0 zero stamps; 3,410/3,410 steps sample-exact, 0 errors, 0 non-increasing |
| Hops | 5/5 `ok`; 683 packets and 245,880 samples each, 0 gaps; Context frequency == plan == Talise readback |
| Span inside a hop | `last_sample_ps − first_sample_ps` = 1,000,484,212 ps on every hop = 245,879 samples at 245.76 MSPS |
| Loss | framer 3,420 forwarded, 0 dropped; NIC `rx_missed_errors` 0; 0 `pkt_count` gaps |

Report-only observations:

- **VRT against Tick spacing.** Consecutive hops are 200 ms apart on Tick. Their VRT first-sample
  spacing is longer by 1.63, 1.60 and 1.50 µs for hops 1 to 4, about 8 ppm. Corundum's PTP PPS
  and Tick's clock come from different oscillators, so this drift is expected. It is the only
  cross-check between the two clocks; nothing maps one onto the other.
- **The priming hop** (0 → 1) is 62.8 µs longer than Tick's spacing. Not yet explained.
- **No PPS edge fell inside a 1 ms capture** this run, so the step across a PPS re-anchor is
  covered by unit tests only.

What the timestamps mean: seconds are the host's UTC at arm (±1 s), and the sub-second phase is
Corundum's free-running PTP PPS. They are sample-coherent within and across hops, not GNSS UTC.

## Boot

tron stopped booting from SD on 2026-10-25: two SD power-ons printed nothing on the console for
300 s, while vrt49's Corundum CI booted the same board over JTAG between them. The SD card's
stock files were unchanged (BOOT.BIN `5d4dc4e8…`). The launcher now defaults to JTAG
(`scripts/run-tron-hopcap.sh --boot jtag`): `system_top.bit` and `psu_init.tcl` come from the
build's XSA, and the kernel and DTB are staged beside the stock files. The first failed SD boot
also showed that the session left tron powered after a failed boot; that is fixed (`9a6eab1`),
and the second failed boot powered the board off.

*2026-10-26:* the default is now `--boot auto`, which follows the place's `boot-strategy` tag
(`BootZynqMPJTAG` today). SD boot stays supported for an SD-strapped board (tag `BootFPGASoC`).

## Evidence

`nemo:~/hopcap-artifacts/tsfix-20261025-2130/`: `manifest.json` (with `time_base`), spool,
SigMF, JUnit, `board-session.json`, `jtag-inputs.txt` (bitstream, psu_init and XSA hashes).
`verify_evidence.py run` passed. The two failed SD attempts are `tsfix-20261025-2051/` and
`tsfix-20261025-2114/`.
