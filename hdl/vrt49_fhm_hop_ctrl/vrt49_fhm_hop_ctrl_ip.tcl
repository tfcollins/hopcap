# SPDX-License-Identifier: BSD-2-Clause
source ../../scripts/adi_env.tcl
source $ad_hdl_dir/library/scripts/adi_ip_xilinx.tcl
adi_ip_create vrt49_fhm_hop_ctrl
adi_ip_files vrt49_fhm_hop_ctrl [list "vrt49_fhm_hop_ctrl.v"]
adi_ip_properties vrt49_fhm_hop_ctrl
ipx::save_core
