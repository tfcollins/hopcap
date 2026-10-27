# tron first composite run — 2026-10-23

**Image:** bitstream attempt 3 (`e77d095f…`, review-fixed fragment) + boot set from its XSA
(BOOT.BIN `20b16810…`, Image `11478997…`, system.dtb `100d7034…`), SD-staged by the harness,
stock trio restored afterwards (`board-session.json`: `restored_matches_stock: true`).
**Plan:** `band-2400-2500-4slot-slow` (200 ms period, 200 µs settle, 1 ms capture), 5 edges.
**Host:** nemo, `iio_attr` CLI transport (measured round trip 14.7 ms → staging floor 88 ms).

## What worked on silicon

| Stage | Result |
|---|---|
| Boot + identity | hop controller `HOP2` v1.0 at 0x84AC0000; Tick 100 MHz, depth 16, GPIO capable; `vrt49_framer`, `adrv9009-phy`, `axi-timed-command-scheduler` present |
| Tick | 5/5 rising edges `EXECUTED`, `executed == requested` on every edge (0-tick skew) |
| Talise FHM | readback after each edge: 2450, 2410, 2440, 2470, 2490 MHz — exactly the one-behind pipeline model |
| Hop controller | `event_count` 5, `epoch` 5, `capture_count` 5 (all windows closed by `CAPTURE_TICKS`), `superseded` 0, no drain timeout, ended `CLOSED`, `last_slot` 4 |
| Framer / UDP | 3420 packets forwarded, 0 dropped, 0 UDP errors = 5 × (683 IF Data + 1 Context) for 1 ms at 245.76 MSPS |
| Negative control | staging without an edge did not move the PLL and emitted no Context |
| Card | staged sums verified live, stock trio restored and verified |

## What did not

- **Host reception: 0 datagrams spooled.** nemo's XL710 received 3088 frames (401 `rx_missed_errors`
  in the ring during the 8.4 Gb/s bursts), and the kernel dropped 3019 in
  `TcpExtIPReversePathFilter`. Cause: 192.0.2.2 lives on the SFP interface (`vrt49-sfp` profile)
  and the XL710 had **no IPv4 address**; Linux drops every packet arriving on an address-less
  interface when its `rp_filter` is non-zero (loose mode included). The `vrt49-qsfp` profile
  cannot be activated without carrier, and carrier only exists once the FPGA image is up.
  Fix needs root on nemo: `sysctl -w net.ipv4.conf.enp4s0f0np0.rp_filter=0` (persist in
  `/etc/sysctl.d`) or an address on the XL710; `ufw` is active and may also need
  `allow in on enp4s0f0np0 … 4991/udp`.
- The 50 ms example plan is refused by the staging-floor check with this transport; the 200 ms
  variant runs. A persistent IIO context (pylibiio against libiio 0.26, or running the hop loop on
  the board) is the way to shorter periods.
- `late_count_delta` 1: the park-trigger-low command had a 1 ms lead against a 14.7 ms transport;
  fixed to 4× the measured round trip with `LATE_EXECUTE_NOW`.

## Evidence
`nemo:~/hopcap-artifacts/run5/` (manifest, spool (empty), SigMF, JUnit, board-session.json);
copied to `hopcap/evidence/run5/` on picard. Earlier attempts in the same session: run1 (launcher
checked the lock it had taken), run2 (system `adi-lg` could not see the place), run3 (board address
resolved to 192.0.2.1), run4 (staging floor refused the 50 ms plan).

## Run 7 — end to end (after `rp_filter=0` + ufw allow on nemo)

`tests/hardware`: boot/identity **PASSED**, board→host UDP probe **PASSED**, negative control
**PASSED**, hop/capture plan **FAILED only on the ≥95 % completeness gate** (3/5 hops complete).

| Per hop | 0 (priming) | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| Context RF (MHz) == Talise readback == planned | 2450 | 2410 | 2440 | 2470 | 2490 |
| epoch | 1 | 2 | 3 | 4 | 5 |
| Tick skew (ticks) | 0 | 0 | 0 | 0 | 0 |
| packets received / 683 | 683 | 593 | 683 | 595 | 683 |
| samples (expected 245760 ± 360) | 245880 | 213480 | 245880 | 214200 | 245880 |
| disposition | ok | incomplete | ok | incomplete | ok |

- 5 Contexts received, each immediately before its capture; `epochs_seen` 1..5, no skips, no orphans, 0 malformed, single peer 192.0.2.1:4991.
- FPGA: 3420 packets forwarded, 0 dropped, 0 UDP errors (again exactly 5 × 684). Tick `late_count` Δ 0.
- Host: 3242 datagrams spooled, `rx_missed_errors` Δ **178** on the XL710 ring (default ring 512) during the 8.4 Gb/s bursts — the two incomplete hops lost 90 and 88 packets each, all in the NIC. `Udp RcvbufErrors` Δ 0: the C spooler kept up; the ring did not.
- Complete hops carry 683 packets = 245 880 samples for a 1 ms window: the framer finishes the partial last packet (+120 samples), inside the designed ±1-packet tolerance.
- First real I/Q: `hops/hop_0002.npy`, 245 880 CI16 samples at 2440 MHz; I/Q RMS ≈ 19.8k, peaks ±28k (what the ADC saw; RF input not characterised).
- SigMF has five capture segments (complete and incomplete hops) tagged with their Context frequency; `verify_evidence.py run` flagged two stale hashes because the manifest and launcher log were amended after the checksum file was written — fixed (checksums rewritten after the final manifest, operator logs excluded).

**Next:** NIC ring tuning on nemo (root: `ethtool -G enp4s0f0np0 rx 8160`, as in vrt49's QSFP
qualification) should remove the ring loss; the receiver can also be pinned (`HOPCAP_RX_CPU`).
