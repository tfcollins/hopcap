# Compile a plan offline

Nothing here needs hardware. A plan is a small JSON file in human units:

```json
{
  "name": "band-2400-2500-4slot",
  "frequencies_hz": [2410000000, 2440000000, 2470000000, 2490000000],
  "sample_rate_hz": 245760000,
  "period_us": 50000,
  "settle_us": 200,
  "capture_us": 1000,
  "pulse_us": 10000,
  "lead_us": 50000
}
```

Compile it:

```sh
pip install -e third_party/tick -e .
hopcap plan --plan examples/plans/band-2400-2500-4slot.json --dry-run
```

The JSON that comes back contains:

- the **slot table**: up to eight Q44.20 frequency words, slot 0 always the Talise
  `fhmInitFrequency` (2450 MHz by default);
- one **edge per hop plus one priming edge**: which frequency each edge commits, which frequency
  the host must have staged before it, the rise/fall ticks, and the two Tick descriptors as the
  twelve hex words the driver accepts;
- the **timing check**: the period must exceed settle + capture + pulse + a Context margin.

Edge 0 commits 2450 MHz, not the first planned frequency. That is the Talise FHM pipeline, not
a bug; see [FHM pipeline](../explanation/fhm-pipeline.md).

The golden output for the example plan lives in `tests/fixtures/` and is pinned by a unit test.
