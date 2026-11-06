# SPDX-License-Identifier: MIT
#
# hopcap block-design fragment: vrt49_fhm_hop_ctrl between Tick and the vrt49 framer, and
# tick_pps_select driving Tick's pps_in.
#
# Sourced from the staged adrv9009zu11eg_vrt49_corundum/system_bd.tcl AFTER
# system_bd_vrt49.tcl (framer + tie-offs) and AFTER Tick's
# tick_adrv9009zu11eg_adrv2crr_fmc.tcl (axi_timed_command_scheduler_0 @0x46000000,
# tick_fhm_gpio_slice -> tick_fhm_gpio port -> TAL_GPIO_0 via the system_top patch).
#
# Everything here runs on sys_cpu_clk (100 MHz), the same clock as Tick and the framer.
# The only clock crossing is into tick_pps_select (Corundum's PTP-domain PPS and the
# external PPS pin), handled by its own two-flop synchronisers; system_tick_pps_cdc.xdc
# cuts their first stage.  Address 0x84AC0000 is the first free aperture above vrt49's
# 0x84A8..0x84AB blocks (ad_cpu_interconnect passes >=0x80000000 through unchanged on
# ZynqMP).

set hop_ctrl_base 0x84AC0000

# Until vrt49 grows a VRT_FABRIC_HOP_ENABLE hook, free the framer's fabric pins from
# the constants system_bd_vrt49.tcl ties them to (same net-deletion pattern that file
# uses for axi_tx_fifo).
if {![info exists ad_project_params(VRT_FABRIC_HOP_ENABLE)] || !$ad_project_params(VRT_FABRIC_HOP_ENABLE)} {
  # vrt49_zero_32/vrt49_zero_64 also feed the framer's PTP inputs, so only detach our pins
  # from the shared constant nets (never delete the nets themselves).
  foreach pin {fabric_sample_enable fabric_context_valid fabric_rf_frequency_q44_20 fabric_hop_epoch} {
    set net [get_bd_nets -quiet -of_objects [get_bd_pins axi_vrt49_framer_0/$pin]]
    if {$net ne ""} {
      disconnect_bd_net $net [get_bd_pins axi_vrt49_framer_0/$pin]
    }
  }
  # vrt49_one_1's only sink was fabric_sample_enable; remove the now-dangling constant.
  if {[llength [get_bd_cells -quiet vrt49_one_1]]} {
    delete_bd_objs [get_bd_cells vrt49_one_1]
  }
}

ad_ip_instance vrt49_fhm_hop_ctrl vrt49_fhm_hop_ctrl_0 [list \
  DEFAULT_SETTLE_TICKS 2000 \
  DEFAULT_CAPTURE_TICKS 0 \
  DEFAULT_IDLE_OPEN 1]
ad_connect sys_cpu_clk    vrt49_fhm_hop_ctrl_0/clk
ad_connect sys_cpu_resetn vrt49_fhm_hop_ctrl_0/resetn

# The same 4-bit slice leaves the BD as tick_fhm_gpio (bit 0 -> TAL_GPIO_0) and feeds
# the controller: bit 0 = hop event, bits 3:1 = slot index.
ad_connect tick_fhm_gpio_slice/Dout vrt49_fhm_hop_ctrl_0/scheduler_gpio

ad_connect vrt49_fhm_hop_ctrl_0/stream_enable              axi_vrt49_framer_0/fabric_sample_enable
ad_connect axi_vrt49_framer_0/fabric_samples_drained       vrt49_fhm_hop_ctrl_0/fabric_samples_drained
ad_connect vrt49_fhm_hop_ctrl_0/fabric_context_valid       axi_vrt49_framer_0/fabric_context_valid
ad_connect axi_vrt49_framer_0/fabric_context_ready         vrt49_fhm_hop_ctrl_0/fabric_context_ready
ad_connect vrt49_fhm_hop_ctrl_0/fabric_rf_frequency_q44_20 axi_vrt49_framer_0/fabric_rf_frequency_q44_20
ad_connect vrt49_fhm_hop_ctrl_0/fabric_hop_epoch           axi_vrt49_framer_0/fabric_hop_epoch
ad_connect axi_vrt49_framer_0/fabric_context_accepted      vrt49_fhm_hop_ctrl_0/fabric_context_accepted

ad_cpu_interconnect $hop_ctrl_base vrt49_fhm_hop_ctrl_0

# ---------------------------------------------------------------------------------------
# Tick pps_in: qualified external 1PPS, falling back to Corundum's PTP PPS.
#
# Tick's fragment (TICK_SYNCHRONA_PPS unset) ties pps_in to tick_pps_tieoff (xlconstant 0).
# Replace that with tick_pps_select_0 (hdl/tick_pps_select, docs/explanation/pps.md):
#   int_pps_async <- corundum_hierarchy/corundum_core/ptp_pps_str  (same net as
#                    vrt49_time_base_0/pps_in: Tick and VRT share Corundum's second)
#   ext_pps_async <- vrt49_external_pps_slice/Dout = gpio_i[16] = PMOD0_D4 (1.8 V), the pin
#                    vrt49 already uses for its selectable external PPS; no new pins.
if {[llength [get_bd_cells -quiet tick_pps_pmod0_slice]]} {
  error "hopcap: Tick was built with TICK_SYNCHRONA_PPS=1; tick_pps_select owns pps_in, unset it"
}
set tick_pps_net [get_bd_nets -quiet -of_objects [get_bd_pins axi_timed_command_scheduler_0/pps_in]]
if {$tick_pps_net ne ""} {
  delete_bd_objs $tick_pps_net
}
if {[llength [get_bd_cells -quiet tick_pps_tieoff]]} {
  delete_bd_objs [get_bd_cells tick_pps_tieoff]
}

# Defaults are the production values at 100 MHz (1.25 s timeout, 0.9..1.1 s periods,
# 2 good periods, 100 ms output pulse); listed so the BD records them.
ad_ip_instance tick_pps_select tick_pps_select_0 [list \
  ALIVE_TIMEOUT 125000000 \
  PERIOD_MIN 90000000 \
  PERIOD_MAX 110000000 \
  GOOD_PERIODS 2 \
  PULSE_CYCLES 10000000]
ad_connect sys_cpu_clk    tick_pps_select_0/clk
ad_connect sys_cpu_resetn tick_pps_select_0/resetn
ad_connect corundum_hierarchy/corundum_core/ptp_pps_str tick_pps_select_0/int_pps_async
ad_connect vrt49_external_pps_slice/Dout tick_pps_select_0/ext_pps_async
ad_connect tick_pps_select_0/pps_out axi_timed_command_scheduler_0/pps_in

# Status to the PS on EMIO gpio_i[20] (sel_ext) and gpio_i[21] (ext_alive): ZynqMP gpio
# controller lines 98 and 99 (EMIO base 78).  Evidence that both bits are free in this
# design (ADI hdl e57851ffc, vrt49 5e20550, linux 2e8908932):
#   * every top this project builds (vrt49 system_top_sfp.v; ADI adrv2crr_fmc
#     system_top_corundum.v and system_top.v) has `assign gpio_i[21:20] = gpio_o[21:20];`:
#     a PS loopback with no pin (the old fan_pwm/fan_tach bits; the fan now sits on
#     axi_fan_control_0 through the axi_fan_* BD ports);
#   * no BD script (adrv9009zu11eg_bd.tcl, adrv2crr_fmc_bd.tcl, Corundum, vrt49, Tick)
#     slices gpio_i/gpio_o bits 20/21; the only gpio_i consumers are sys_ps8/emio_gpio_i,
#     vrt49_external_pps_slice (bit 16) and Tick's opt-in SYNCHRONA slice (bit 12);
#   * the boot device tree (zynqmp-adrv9009-zu11eg-reva-adrv2crr-fmc-reva-jesd204-fsm and
#     its includes, plus the vrt49 and hopcap overlays) references EMIO lines 78..89, 94 and
#     130..170, never 98 or 99.
# The board's gpio_i port keeps feeding vrt49_external_pps_slice; only the PS input is
# rebuilt as {gpio_i[94:22], ext_alive, sel_ext, gpio_i[19:0]} (xlconcat In0 is the LSB).
set emio_net [get_bd_nets -quiet -of_objects [get_bd_pins sys_ps8/emio_gpio_i]]
if {$emio_net eq ""} {
  error "hopcap: sys_ps8/emio_gpio_i is not connected; cannot splice the PPS status bits"
}
disconnect_bd_net $emio_net [get_bd_pins sys_ps8/emio_gpio_i]
ad_ip_instance xlslice tick_pps_gpio_lo [list \
  DIN_WIDTH 95 DIN_FROM 19 DIN_TO 0 DOUT_WIDTH 20]
ad_ip_instance xlslice tick_pps_gpio_hi [list \
  DIN_WIDTH 95 DIN_FROM 94 DIN_TO 22 DOUT_WIDTH 73]
ad_connect gpio_i tick_pps_gpio_lo/Din
ad_connect gpio_i tick_pps_gpio_hi/Din
ad_ip_instance xlconcat tick_pps_gpio_concat [list \
  NUM_PORTS 4 IN0_WIDTH 20 IN1_WIDTH 1 IN2_WIDTH 1 IN3_WIDTH 73]
ad_connect tick_pps_gpio_lo/Dout        tick_pps_gpio_concat/In0
ad_connect tick_pps_select_0/sel_ext    tick_pps_gpio_concat/In1
ad_connect tick_pps_select_0/ext_alive  tick_pps_gpio_concat/In2
ad_connect tick_pps_gpio_hi/Dout        tick_pps_gpio_concat/In3
ad_connect tick_pps_gpio_concat/dout    sys_ps8/emio_gpio_i

validate_bd_design
puts "hopcap: vrt49_fhm_hop_ctrl_0 at $hop_ctrl_base, event/slot from tick_fhm_gpio_slice, fabric nets to axi_vrt49_framer_0"
puts "hopcap: Tick pps_in <- tick_pps_select_0 (ext gpio_i\[16\], fallback Corundum ptp_pps_str); status on EMIO gpio_i\[21:20\]"
