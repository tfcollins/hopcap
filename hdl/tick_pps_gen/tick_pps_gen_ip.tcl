# SPDX-License-Identifier: MIT
#
# tick_pps_gen Vivado IP packaging script (AXI-less: the _lite properties, as
# tick_pps_select_ip.tcl does).
source ../../scripts/adi_env.tcl
source $ad_hdl_dir/library/scripts/adi_ip_xilinx.tcl
adi_ip_create tick_pps_gen
adi_ip_files tick_pps_gen [list "tick_pps_gen.v"]
set_property top tick_pps_gen [current_fileset]
adi_ip_properties_lite tick_pps_gen
set_property display_name "hopcap Tick internal 1PPS" [ipx::current_core]
set_property description "1 Hz pulse of TICK_HZ tick-clock cycles for Tick and vrt49_time_base pps_in (no GPS)" [ipx::current_core]
ipx::save_core
