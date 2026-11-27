# ADRV9361-Z7035 hops land on their Tick timestamps — 2026-11-26

**Result:** 16/16 hardware tests pass over 3 fresh boots on place `lablp`. On every boot all 16
scheduled hops were measured, both RX and TX hops landed 2.30–10.81 µs after their scheduled Tick
timestamp, inside the 1–15 µs window, and Tick's late and overflow counters did not move. This
is the run that gated hopcap #16 (the `adrv9361` profile) into `main`.

**Image:** hopcap `4ff0be8` (merged as `57b658f`), Tick `732a5cb`, vrt49 `0c8764d`; bitstream
and modules built by `hw-test-adrv9361.yml` on the nemo runner. The modules target ADI Linux
`2023_r2_p1` (`6.1.70-284114-g2e8908932dfd`) and the stock Kuiper SD card, and the bitstream is
loaded at runtime. **Plan:** `examples/plans/adrv9361-loopback-8slot.json` (eight `(rx_lo, tx_lo)`
slots near 2.4 GHz, 1.25 s period, 15.36 MSPS; RX-only, TX-only and RX+TX hops).
**CI:** [run 45842846376](https://github.com/tfcollins/hopcap/actions/runs/45842846376), attempt 2.

## What is measured

Each boot is its own `adi-lg request --power-down` session: cold power-up, runtime PL load, a
gated run and a gapless run of the plan. TX1 carries a DDS tone that reaches RX1 over the
loopback, so each slot has its own tone in RX baseband. A hop's latency is the sample where the
new slot's tone overtakes the old one, mapped to Tick time, minus the edge's scheduled Tick
tick. The how-to, [Run on the ADRV9361-Z7035](../how-to/adrv9361-lablp.md), describes the
method and the checks.

Rises are scheduled without REPORT_COMPLETION, so each edge is `SCHEDULED` at its requested
tick. The run proves the edges fired on time from Tick's late and overflow counters and the
tap's drop counter, which must all stay at 0.

## Results

RX counts RX-only and RX+TX hops (10 per boot). TX counts TX-only and RX+TX hops (12 per boot).

| Boot | Hops measured | RX median / max | TX median / max | All hops min / mean / max | Old-tone margin before the edge |
|---|---|---|---|---|---|
| 1 | 16/16 | 3.99 / 6.76 µs | 5.25 / 7.58 µs | 2.30 / 4.94 / 7.58 µs | ≥ 12.9 dB |
| 2 | 16/16 | 6.48 / 10.81 µs | 5.19 / 10.46 µs | 2.72 / 6.35 / 10.81 µs | ≥ 13.6 dB |
| 3 | 16/16 | 3.85 / 7.61 µs | 3.83 / 6.57 µs | 2.63 / 4.25 / 7.61 µs | ≥ 13.5 dB |

| Check | Result |
|---|---|
| Tests | 16 passed, 0 failed, 0 skipped: 5 per-boot tests × 3 boots, plus `test_repeat_boots_agree` |
| Tick counters | late and overflow deltas 0 on every boot |
| Gated run | 16/16 windows per boot; each window's FFT peak is its slot's tone, so RX and TX both switched |
| Early hops | none: the old tone dominates by at least 12.9 dB before every edge |
| Across boots | per-boot means 4.25–6.35 µs, inside `boot_spread_ns` |

The tests are:

- `test_gapless_every_hop_lands_on_its_scheduled_tick`
- `test_gapless_rx_and_tx_hops_both_land`
- `test_gapless_no_hop_without_an_edge`
- `test_gated_contexts_and_windows`
- `test_gated_windows_carry_the_slots_rx_and_tx_tone`
- `test_repeat_boots_agree`

## Across runs

The earlier proof run, [45763737508](https://github.com/tfcollins/hopcap/actions/runs/45763737508),
also passed 16/16 over 3 boots, with per-boot means of 4.59, 5.44 and 4.29 µs. Across both runs
every hop landed between 2.2 and 13.4 µs. The 13.37 µs maximum came from that earlier run and
leaves about 1.6 µs of margin to the window. The spread comes from synthesizer settling, which
is slow on the same transitions on every boot. It is not jitter in when the hop command fires.

## Report-only observations

- The tone onset (the old tone down 6 dB) is recorded but not gated. Two TX-only hops in boot 1
  had onsets of 27.4 µs and 19.9 µs, while their crossings landed in the window. An earlier run
  had a similar onset outlier.
- Attempt 1 of the CI run failed before any board session, on an SSH host-key check on the
  runner. Attempt 2 ran the same commit after the runner was fixed.
