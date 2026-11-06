# Tick's 1PPS: external with a Corundum fallback

Tick aligns its device time to a 1PPS edge: software writes `TIME_EPOCH`, arms it, and the
next rising edge on `pps_in` loads the epoch and sets `time_aligned` in `PPS_STATUS`. Before
this change the hopcap image tied `pps_in` to a constant (`tick_pps_tieoff`), so Tick never
saw an edge and could never align. An external 1PPS might not be wired up on the bench, but
the system still needs a second boundary. `tick_pps_select_0` (RTL in
`hdl/tick_pps_select/`) now drives `pps_in` and picks between two sources:

| Source | Net | Domain | When used |
|---|---|---|---|
| external | `vrt49_external_pps_slice/Dout` = EMIO `gpio_i[16]` = PMOD0_D4 (1.8 V) | asynchronous pin | only while qualified (below) |
| internal | `corundum_hierarchy/corundum_core/ptp_pps_str` | Corundum PTP clock | reset default and fallback |

These are the nets vrt49's time base already uses (`pps_alt_in` and `pps_in`), so the design
adds no pins. PMOD0_D4 is a 1.8 V bank: an external 3.3 V or 5 V PPS must go through a level
translator.

```{mermaid}
flowchart LR
  pin["PMOD0_D4<br/>gpio_i[16]"] --> slice[vrt49_external_pps_slice]
  cor["corundum_core<br/>ptp_pps_str"] --> tb[vrt49_time_base_0 pps_in]
  slice --> tb2[vrt49_time_base_0 pps_alt_in]
  slice -->|ext_pps_async| sel[tick_pps_select_0]
  cor -->|int_pps_async| sel
  sel -->|pps_out| tick[axi_timed_command_scheduler_0 pps_in]
  sel -->|"sel_ext, ext_alive"| emio["PS EMIO gpio_i[21:20]"]
```

## Selection rule

The selector runs on `sys_cpu_clk` (100 MHz, Tick's clock). It double-synchronises both inputs
and watches their rising edges all the time, whichever one is selected.

- **External qualifies** (`ext_alive`) after `GOOD_PERIODS` (2) consecutive edge-to-edge periods
  that each fall inside [`PERIOD_MIN`, `PERIOD_MAX`] (0.9 s to 1.1 s), and only while an
  external edge has arrived within the last `ALIVE_TIMEOUT` (1.25 s). Any period outside the
  window, or a timeout, resets the count. A floating pin, chatter, glitches or a wrong-rate
  source therefore never qualify, and a source that drifts out of range must earn two more
  good periods before it is used again.
- **The applied source follows `ext_alive`** (`sel_ext` = 1 means external). It changes only in
  a quiet cycle, where both synchronised inputs are low in this sample and the previous one. A
  stuck-high external that has just died, or a wide-duty external in anti-phase with
  Corundum's ~54 % high level, could hold off such a cycle forever. So once a switch has been
  waiting `PERIOD_MAX`, the selector switches in the first cycle where the *target* input
  alone is quiet.
- **Reset selects internal.** Qualification starts from zero.

All four thresholds and the 100 ms output pulse width (`PULSE_CYCLES`) are IP parameters. The
block design sets them explicitly in `hdl/system_bd_fabric_hop.tcl`.

## The no-spurious-edge guarantee

`pps_out` is regenerated, not muxed. When the selector accepts a rising edge of the selected
input, it starts a fixed-width pulse with the same latency for either source. As a result:

- Every rising edge Tick sees comes from a rising edge of the input selected at that moment.
  A switch can never create an edge. Both edge detectors always run, so an input that is
  already high when it becomes selected shows no edge until it goes low and rises again.
- Consecutive edges to Tick are at least `PERIOD_MIN` (0.9 s) apart. If the selected input
  produces an edge sooner than that after the last output edge, the selector drops it. This
  covers two cases: the phase step when switching between two unsynchronised sources, and a
  glitch on a selected external that has not yet been disqualified.
- At worst one second's edge is lost at a switch. A spurious or duplicated edge is never
  delivered.
- After reset no edge is emitted until an input has been seen low and then rises.

One limit is inherent: if a glitch on a qualified external lands inside the period window, it
looks exactly like a real edge. It is delivered, and the following real edge then
disqualifies the source.

The cocotb bench `hdl/sim/cocotb/test_tick_pps_select.py` (`make -C hdl/sim/cocotb
TOP=tick_pps_select`) checks every trace against these properties. It sweeps the phase between
the two sources, including near-coincident edges and the anti-phase case, and covers floating,
glitching, out-of-range, stopped and stuck-high externals and reset. Mutants that remove the
spacing guard, the quiet-cycle rule, the timeout, either period bound or the two-period
requirement all fail it.

## Status bits for software

| EMIO bit | ZynqMP GPIO line (`gpio@ff0a0000`) | Signal | Meaning |
|---|---|---|---|
| `gpio_i[20]` | 98 | `sel_ext` | 1 = Tick's `pps_in` currently comes from the external pin |
| `gpio_i[21]` | 99 | `ext_alive` | 1 = the external input is qualified (the source the selector wants) |

Read them with libgpiod, for example `gpioget <chip> 98 99`, where `<chip>` is the
`zynqmp_gpio` controller listed by `gpiodetect`. Line numbers are offsets on that controller;
the EMIO lines start at 78. `ext_alive=1, sel_ext=0` lasts only until the next quiet cycle.

Why these two bits are free in this design (ADI HDL `e57851ffc`, vrt49 `5e20550`, Linux
`2e8908932`):

- Every top-level this project builds has `assign gpio_i[21:20] = gpio_o[21:20];`, a PS
  loopback with no pin. These are vrt49's `system_top_sfp.v` and ADI's `system_top_corundum.v`
  and `system_top.v`. The bits used to carry `fan_pwm`/`fan_tach`; the fan now sits on
  `axi_fan_control_0`.
- No block-design script slices `gpio_i` or `gpio_o` bits 20 or 21. The only `gpio_i`
  consumers are `sys_ps8/emio_gpio_i`, `vrt49_external_pps_slice` (bit 16) and Tick's opt-in
  SYNCHRONA slice (bit 12).
- The boot device tree (`zynqmp-adrv9009-zu11eg-reva-adrv2crr-fmc-reva-jesd204-fsm` and its
  includes, plus the vrt49 and hopcap overlays) references EMIO lines 78–89, 94 and
  130–170, never 98 or 99.

The block design rebuilds only the PS input as
`{gpio_i[94:22], ext_alive, sel_ext, gpio_i[19:0]}`. The board-level `gpio_i` port still feeds
`vrt49_external_pps_slice` unchanged.

## What "aligned" means with nothing connected

With no external PPS, Tick's `time_aligned` means aligned to **Corundum's free-running PTP
second**: the rollover of the NIC's PTP hardware clock. It is not GNSS time. Unless `ptp4l` or
another servo disciplines that clock, the second is only as accurate as the PTP reference
clock, and its absolute value is whatever the clock was set to. `time_aligned` and the
`pps_seen` flag say an edge arrived. They do not say the edge is traceable to UTC.

## Tick time and VRT timestamps

vrt49's time base (`vrt49_time_base_0`) uses the same `ptp_pps_str` net as its reset-default
`corundum` source. With no external PPS (or one that never qualifies), Tick's second and the
integer-seconds boundary of the VRT timestamps are the same Corundum edge, so a Tick hop time
and a VRT timestamp share one second boundary.

They diverge in one case. When a qualified external PPS is present, Tick switches to it on
its own, but the VRT time base stays on `corundum` until the host selects
`v49_tb_pps_source=external` (see `src/hopcap/framer.py`). To keep the two on one boundary
with an external PPS connected, select `external` on the time base as well, and re-arm both
once `sel_ext` reads 1.

A switch never re-aligns Tick by itself. Tick's counter keeps running, and the epoch loads
only on an armed edge. After `sel_ext` changes, the second boundary has moved to the new
source's phase, so software must write `TIME_EPOCH` and re-arm to realign.
