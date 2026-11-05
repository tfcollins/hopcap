# Measure the Talise hop settle time

A plan's `settle_us` is the delay the hop controller waits after each FHM edge before it opens
the capture. This page measures how long the Talise LO actually takes to settle, so that number
can replace the configured one (datax_sdk_sdr#9, REQ-TEST-031).

```{admonition} Status
:class: warning
The analysis is implemented and tested on synthetic signals. No hardware measurement has been
made yet: tron has no RF reference cabled.
```

## Why a fixed reference is needed

Talise's TX and RX share one RF PLL. A TX tone leaking into RX moves with the LO, so it hides the
LO's settling, and an internal loopback skips the LO entirely. The measurement needs an RF tone
that does **not** hop. Then, after each hop, the tone appears at the baseband offset
`f_ref - f_LO`, and the LO's settling shows up as a frequency and phase transient of that tone.

## 1. Cable a reference into chip A RX1

Pick one of these setups:

- **Chip B, fixed LO (no new equipment).** On the ZU11EG, set chip B's TX LO to a fixed frequency
  and transmit a CW tone (DDS) into chip A's RX1 through an SMA cable with **30-40 dB of
  attenuation**. Only chip A hops, because hopcap drives Talise A's FHM GPIO.
- **Bench signal generator** into chip A's RX1, at a level that keeps the ADC well below full
  scale.

Use a manual RX gain so that AGC does not move during the capture.

## 2. Choose the reference frequency with the plan

`examples/plans/settle-measure.json` hops 2420, 2440, 2460 and 2480 MHz. It uses `settle_us: 0`,
a 2 ms capture (491,520 samples at 245.76 MSPS) and the 200 ms period of the `-slow` plan, which
is above tron's host staging floor.

Edge 0 is the priming hop. It commits `fhmInitFrequency`, 2450 MHz. A reference at exactly
2450 MHz would land at DC on that hop, where LO leakage, DC correction and the I/Q image all sit,
so the analysis refuses it (`tone_near_dc`). Use **2452.5 MHz**. Each LO then sees the tone at:

| LO (MHz) | Offset (MHz) |
|---|---|
| 2450 (priming) | +2.5 |
| 2420 | +32.5 |
| 2440 | +12.5 |
| 2460 | -7.5 |
| 2480 | -27.5 |

All of these are inside the default usable band, +/-0.8 x fs/2 = +/-98.3 MHz.

If you change the plan, keep every LO, including the priming one, at least 1 MHz from the
reference (`dc_guard_hz`) and inside that band.

## 3. Run the plan

Run it the usual way (see the [runbook](tron-nemo-runbook.md)), passing
`--plan examples/plans/settle-measure.json`. Repeat the run a few times. The analysis takes
several run directories and reports one summary over all of them.

## 4. Analyse

```sh
hopcap settle out/settle-1 out/settle-2 out/settle-3 --ref-hz 2452500000
hopcap settle out/settle-1 --ref-hz 2452500000 --tol-hz 1000 --json > settle.json
```

For each hop, `hopcap.settle` does the following:

1. Finds the tone in the last quarter of the capture (`--tail-fraction`) and estimates its SNR.
   The search is +/-200 kHz around the expected offset, and the SNR is tone power over noise
   power across the full sample rate.
2. Mixes the tone to 0 Hz and low-passes it with a boxcar of `--window` samples (default 256,
   about 1 us). It then averages the lag-1 phase difference over another window, which gives
   the instantaneous frequency.
3. **Settle**: the first time after which the frequency stays within `--tol-hz` (default 1 kHz)
   of its final value. The final value is the median over the tail.
4. **Phase settle**: the same test on the unwrapped phase, measured against a straight line
   fitted to the tail, with tolerance `--phase-tol-deg` (default 5 degrees).

The summary reports the median and maximum over the non-priming hops, both overall and per LO
step size. The priming hop is reported separately, because its VRT-vs-Tick spacing already
shows an unexplained 62.8 us excess (see `STATUS.md`).

### What the numbers are relative to

Times count from the **first captured sample of the hop**, not from the Tick edge. With
`settle_ticks = 0`, the controller still drains the framer pipe and publishes the Context packet
before it opens the gate. No register records that latency, and Tick and VRT time are not mapped
onto each other. `open_after_edge_min_s` (`settle_ticks / tick_hz`) is therefore only a lower
bound on the open delay.

A hop flagged `settled_at_first_sample` had already settled when the gate opened. For that hop,
the settle time from the edge is bounded only by the unmeasured open latency.

### When a hop is not measured

The analysis never guesses. It reports `ok: false` with a reason in these cases:

- the hop's disposition is not `ok` (the manifest reasons are carried over);
- `tone_out_of_band` or `tone_near_dc`;
- `tone_not_detected` (SNR below `--min-snr-db`, default 10 dB). If a tone sits at the
  mirrored offset, the reason says so; try `--conjugate` for a swapped I/Q;
- `estimate_too_noisy` (tail frequency noise above 0.2 x tol): raise `--window`, the reference
  level, or the tolerance;
- `tail_not_settled` or `never_settled`: the capture is too short for the transient.

`hopcap settle` exits non-zero if no non-priming hop could be measured.
