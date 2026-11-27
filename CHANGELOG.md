# Changelog

Notable changes to hopcap, newest first. Each entry names the pull request that made it.

## Unreleased

### Added

- ADRV9361-Z7035 hop timing report for the lablp CI runs, and `adrv9361-z7035` rows in
  `support.json`: `fastlock-hop`, `stream-gate` and `vrt-udp-1g` are validated at tier 2 by
  `hw-test-adrv9361.yml` (#17).
- `adrv9361` profile on the ADRV9361-Z7035: Tick-timed AD9361 FastLock RX+TX hops, HOP2 capture
  gating, a `tap_accept` time base, and a hardware workflow that loads the bitstream and overlay
  at runtime on lablp. Hops land 2.2–13.4 µs after their scheduled Tick timestamp, inside the
  1–15 µs window (#16).
- `timed-dma` profile: a Tick DMA gate on the ADRV9009-ZU11EG, with Tick on `core_clk_a` and
  its real period (#15).

### Changed

- labgrid-plugins pinned to `53f6235` for the JTAG serial fix (#10).

## 2026-11-06 and earlier

- Tick PPS falls back to Corundum's PTP PPS when there is no external PPS (#9).
- Talise hop settle is measured from a fixed RF reference (#8).
- Physical port parameter, defaulting to tron's SFP+ cage (#7).
- Time-base arm: a latency-tolerant load check and a vrt49 pin with the seconds CDC fix (#5, #6).
- Preflight gates on the effective `rp_filter` (#4).
- `support.json` declares the validated tron features (#3).
- Initial release: a Tick-timed Talise FHM hop with hardware-gated VRT49 capture over QSFP on the
  ADRV9009-ZU11EG (#1, #2).
