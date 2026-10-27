# `manifest.json` (hopcap-manifest/v1)

Top level: `schema`, `generated_utc`, `plan` (ticks plus `slots_hz`, `capture_samples`),
`sample_rate_hz`, `provenance`, `clock_domains`, `counters`, `counts`, `epochs_seen`, `hops`,
`disposition`, and (live runs) `tick`, `hopctrl`, `edges`, `time_base`.

`clock_domains` states explicitly that Tick time and VRT time are **not** mapped onto each other.

`time_base` (live runs) records the framer's PPS time base, which the run arms before it enables
the stream: vrt49's overlay boots it disabled, and a disabled time base stamps every packet 0.

- `armed`: `before` (state found at connect), `armed_seconds` (host UTC second loaded at the next
  PPS edge), `after_arm` (LOCKED, counting from `armed_seconds`). Each state has `mode`,
  `pps_source`, `status`, `lock_state`, `missed`, `resync_sticky`, `seconds`, `nominal_sps`,
  `ps_per_sample_q32`.
- `after`: the same state once the stream has stopped.
- `stamps`: every step between consecutive IF Data packets checked against the sample count
  (`steps_checked`, `step_errors`, `non_increasing`, `zero_stamps`, `first_errors`), and the
  steps that cross a PPS edge, reported with their offset from nominal (`second_crossings`,
  `crossing_residual_ps_min`/`_max`).
- `hop_spacing_residual_ps`: VRT first-sample spacing minus Tick executed-edge spacing for
  consecutive hops. Report-only: the two clocks are independent.

Each `hops[]` record: `index`, `priming`, `slot`, `planned_hz`, `readback_hz`, `context_hz`,
`epoch`, `tick_requested`, `tick_executed`, `tick_status`, `stage_late`, `context_mono_ns`,
`first_sample_ps`, `last_sample_ps`, `first_arrival_mono_ns`, `packets`, `samples`,
`expected_samples`, `gaps`, `lost_packets_estimate`, `complete`, `disposition`, `reasons`.

Dispositions: `ok`; `incomplete` (tagged, but samples or packet-count continuity short);
`invalid` (`no_context`, `edge_not_executed`, `context_frequency_mismatch`, `readback_mismatch`,
`stage_late`, `fractional_context_frequency`).

Alongside: `run.sigmf-data` / `run.sigmf-meta` (one `captures[]` segment per tagged complete hop
with `core:frequency`), `hops/hop_NNNN.npy`, `datagrams.spool`, `SHA256SUMS.json`.
