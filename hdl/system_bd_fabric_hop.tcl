# SPDX-License-Identifier: MIT
#
# hopcap block-design fragment: vrt49_fhm_hop_ctrl between Tick and the vrt49 framer.
#
# Sourced from the staged adrv9009zu11eg_vrt49_corundum/system_bd.tcl AFTER
# system_bd_vrt49.tcl (framer + tie-offs) and AFTER Tick's
# tick_adrv9009zu11eg_adrv2crr_fmc.tcl (axi_timed_command_scheduler_0 @0x46000000,
# tick_fhm_gpio_slice -> tick_fhm_gpio port -> TAL_GPIO_0 via the system_top patch).
#
# Everything here runs on sys_cpu_clk (100 MHz), the same clock as Tick and the framer,
# so there is no CDC in the new logic.  Address 0x84AC0000 is the first free aperture
# above vrt49's 0x84A8..0x84AB blocks (ad_cpu_interconnect passes >=0x80000000 through
# unchanged on ZynqMP).

set hop_ctrl_base 0x84AC0000

# Until vrt49 grows a VRT_FABRIC_HOP_ENABLE hook, free the framer's fabric pins from
# the constants system_bd_vrt49.tcl ties them to (same net-deletion pattern that file
# uses for axi_tx_fifo).
if {![info exists ad_project_params(VRT_FABRIC_HOP_ENABLE)] || !$ad_project_params(VRT_FABRIC_HOP_ENABLE)} {
  foreach pin {fabric_sample_enable fabric_context_valid fabric_rf_frequency_q44_20 fabric_hop_epoch} {
    set net [get_bd_nets -quiet -of_objects [get_bd_pins axi_vrt49_framer_0/$pin]]
    if {$net ne ""} {
      delete_bd_objs $net
    }
  }
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

validate_bd_design
puts "hopcap: vrt49_fhm_hop_ctrl_0 at $hop_ctrl_base, event/slot from tick_fhm_gpio_slice, fabric nets to axi_vrt49_framer_0"
