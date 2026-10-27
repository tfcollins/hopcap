# Decode a spool again

The raw spool (`datagrams.spool`, every datagram with its arrival time) is kept in each bundle so
a run can be re-associated offline, for example after a parser fix:

```sh
hopcap decode --spool out/run1/datagrams.spool --run out/run1/run.json --out out/run1-redecoded
hopcap verify run out/run1-redecoded
```

`run.json` carries the plan in ticks, `t0_ticks`, `tick_hz`, `sample_rate_hz`,
`samples_per_packet`, `epoch_base` and the per-edge Tick results. The decoder never guesses: a
window whose Context was lost stays `invalid`, it is not relabelled from its neighbours.
