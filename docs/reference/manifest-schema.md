# `manifest.json` (hopcap-manifest/v1)

Top level: `schema`, `generated_utc`, `plan` (ticks plus `slots_hz`, `capture_samples`),
`sample_rate_hz`, `provenance`, `clock_domains`, `counters`, `counts`, `epochs_seen`, `hops`,
`disposition`, and (live runs) `tick`, `hopctrl`, `edges`.

`clock_domains` states explicitly that Tick time and VRT time are **not** mapped onto each other.

Each `hops[]` record: `index`, `priming`, `slot`, `planned_hz`, `readback_hz`, `context_hz`,
`epoch`, `tick_requested`, `tick_executed`, `tick_status`, `stage_late`, `context_mono_ns`,
`first_sample_ps`, `last_sample_ps`, `first_arrival_mono_ns`, `packets`, `samples`,
`expected_samples`, `gaps`, `lost_packets_estimate`, `complete`, `disposition`, `reasons`.

Dispositions: `ok`; `incomplete` (tagged, but samples or packet-count continuity short);
`invalid` (`no_context`, `edge_not_executed`, `context_frequency_mismatch`, `readback_mismatch`,
`stage_late`, `fractional_context_frequency`).

Alongside: `run.sigmf-data` / `run.sigmf-meta` (one `captures[]` segment per tagged complete hop
with `core:frequency`), `hops/hop_NNNN.npy`, `datagrams.spool`, `SHA256SUMS.json`.
