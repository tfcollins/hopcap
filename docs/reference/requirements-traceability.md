# DataX radar-tracking requirements this example exercises

The DataX radar-tracking proposal (`datax_radar_tracking`) lists requirement IDs; this table says
where hopcap produces evidence for each. "Report" means the manifest records it without a pass/fail gate.

| Requirement | Where |
|---|---|
| RAD-DATA-001 frequency/context per unit | `context_hz` per hop from the hardware Context |
| RAD-DATA-006 context takes effect at a device-time boundary | Context precedes data; chain cocotb test; `epoch` per hop |
| RAD-DATA-007 gaps | `gaps`, `lost_packets_estimate`, `incomplete` |
| RAD-DATA-008 epoch increment | `epochs_seen`, `counts.epoch_skips` |
| RAD-TIME-001 timestamp names its clock domain | `clock_domains` |
| RAD-TIME-002 distinct times | Tick ticks, VRT ps, host mono/real ns per hop |
| RAD-TIME-007 bounded queues | TickSession lookahead ≤ (depth−1)/2; `overflow_count_delta` |
| RAD-TIME-008 deadline miss | `tick_status`, `late_count_delta`, `stage_late` |
| RAD-TIME-009 Tick markers vs independent measurement | report only: Tick↔VRT not mapped |
| RAD-ARCH-005 capabilities | Tick `capability_bitmap` probe fails closed |
| RAD-ARCH-007 pinned, independently testable dependencies | `pins.json`, `PROVENANCE.md`, submodules |
| RAD-API-007 examples use documented interfaces, emit provenance | IIO attrs + sysfs + `/dev/mem`; `provenance`, `SHA256SUMS.json` |
