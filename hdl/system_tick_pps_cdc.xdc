# SPDX-License-Identifier: MIT
# hopcap: CDC constraints for tick_pps_select_0 (Tick pps_in selector, sys_cpu_clk), and a
# pull-down on its external PPS pin.
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

# An unconnected PMOD0_D4 should read low, not float: weak pull-down on the pin (both the
# SFP and QSFP tops carry the pmod0_d4 port).  hopcap-only; vrt49's constraints for the same
# pin (its pps_alt_in) are untouched, and a real PPS driver overrides the weak pull.
set_property PULLTYPE PULLDOWN [get_ports pmod0_d4]
