# SPDX-License-Identifier: MIT
# hopcap: CDC constraints for tick_pps_select_0 (Tick pps_in selector, sys_cpu_clk).
#
# int_pps_async is Corundum's ptp_pps_str (PTP clock domain) and ext_pps_async is the
# PMOD0_D4 pin (gpio_i[16], unclocked).  Both enter explicit two-flop synchronisers
# (ext_meta/ext_sync, int_meta/int_sync, ASYNC_REG).  Cut only the asynchronous input of the
# first stage, as system_vrt49_cdc.xdc does for vrt49_time_base_0; the meta -> sync hop stays
# timed.  Both sources are ~1 Hz levels, so no bus or skew bound is needed.
set_false_path \
  -to [get_pins -hier -filter {NAME =~ *tick_pps_select_0*ext_meta_reg/D}]
set_false_path \
  -to [get_pins -hier -filter {NAME =~ *tick_pps_select_0*int_meta_reg/D}]
