# SPDX-License-Identifier: MIT
#
# tick_pps_select Vivado IP packaging script (AXI-less: the _lite properties, as
# vrt49_time_base_ip.tcl does).
source ../../scripts/adi_env.tcl
source $ad_hdl_dir/library/scripts/adi_ip_xilinx.tcl
adi_ip_create tick_pps_select
adi_ip_files tick_pps_select [list "tick_pps_select.v"]
set_property top tick_pps_select [current_fileset]
adi_ip_properties_lite tick_pps_select
set_property display_name "hopcap Tick PPS select" [ipx::current_core]
set_property description "Qualified external 1PPS with fallback to Corundum's PTP PPS for Tick pps_in" [ipx::current_core]
ipx::save_core
