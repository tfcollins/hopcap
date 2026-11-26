# SPDX-License-Identifier: MIT
#
# tap_accept Vivado IP packaging script (AXI-less: the _lite properties).
source ../../scripts/adi_env.tcl
source $ad_hdl_dir/library/scripts/adi_ip_xilinx.tcl
adi_ip_create tap_accept
adi_ip_files tap_accept [list "tap_accept.v"]
set_property top tap_accept [current_fileset]
adi_ip_properties_lite tap_accept
set_property display_name "hopcap tap accepted-beat pulse" [ipx::current_core]
set_property description "One pulse per beat vrt49_sample_tap forwarded, for vrt49_time_base sample_valid" [ipx::current_core]
ipx::save_core
