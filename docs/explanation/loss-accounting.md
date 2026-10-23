# Loss accounting

Three places can lose data, and the manifest keeps them apart:

- **FPGA**: framer `v49_tx_dropped_packets`, UDP `v49_udp_drop_count`, `v49_udp_length_error_count`
  deltas — **hard failures**.
- **Host NIC**: XL710 `rx_missed_errors`, UDP `RcvbufErrors`, softnet drops — **reported**. The
  2026-10-21 QSFP qualification measured a residual 1e-5..3e-4 even with a tuned i40e; gating
  (duty < 1) lowers the average load, so zero loss is plausible but not assumed.
- **Lost Context**: one Context per hop means a lost Context makes that hop untaggable. It is
  marked `invalid` / `no_context`; its neighbours are never relabelled.

Per hop, `gaps` counts mod-16 packet-count discontinuities (seeded from the Context's count),
`lost_packets_estimate` is expected minus received packets, and `complete` requires zero gaps and
samples within one packet of `capture_samples`.
