# Architecture

```
host (nemo)                        tron PL (sys_cpu_clk 100 MHz)                      Talise A
hopcap run ── iio_attr ──► axi_timed_command_scheduler_0 @0x86000000
  │ stage frequency_hopping_mode        │ gpio_out[0] ─────────────────────────────► TAL_GPIO_0 (FHM trigger)
  │ (one hop ahead, readback)           │ gpio_out[3:0] ─► vrt49_fhm_hop_ctrl_0 @0x84AC0000
  │ /dev/mem: slots, SETTLE, CAPTURE          IDLE→DRAIN→SETTLE→PUBLISH→OPEN(capture_ticks)→CLOSED
  │                                           fabric_sample_enable / fabric_context_* ─► axi_vrt49_framer_0
  ◄── UDP 192.0.2.1:4991 ◄── Corundum QSFP lane 0 ◄── udp_tx ◄── framer (IF Data + Context)
```

The DataX dwell loop maps onto it as:

| DataX step | hopcap |
|---|---|
| close admission at T0 | Tick edge → controller DRAIN (gate shut, framer finishes its owned packet) |
| select profile with a hard edge | the same Tick edge on TAL_GPIO_0 commits the Talise hop |
| qualified settling guard | `SETTLE_TICKS` |
| publish Context N+1 | PUBLISH: slot frequency + epoch handed to the framer; Context leaves before any data |
| open capture at T2 | OPEN for `CAPTURE_TICKS`, then CLOSED |

The controller never generates the FHM pulse; Tick's validated `gpio_out[0]` path is untouched.
Overlapping events use latest-wins semantics with `SUPERSEDED_COUNT`, and `EPOCH` counts every
event so a consumer can see from an epoch gap that a Context was never published.

Capture length is in Tick ticks; the framer finishes packets atomically, so received samples are
checked within one packet (360 samples) of the expected count.
