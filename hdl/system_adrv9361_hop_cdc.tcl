# SPDX-License-Identifier: MIT
#
# hopcap adrv9361 profile: CDC constraints for hdl/system_bd_adrv9361_hop.tcl.
#
# A Tcl constraint script, not XDC: it needs control flow to refuse missing cells (Vivado
# drops Tcl control flow from XDC, Designutils 20-1307).  system_project.tcl adds it like
# vrt49's system_vrt49_cdc.tcl: constrs_1, USED_IN_SYNTHESIS false (the cells live in
# out-of-context IP and module references), PROCESSING_ORDER LATE.  An `error` here is
# reported by Vivado but does not by itself stop implementation, so check_adrv9361_hop.tcl
# re-checks every exception on the routed design and fails the build if one is missing.
#
# Clocks: Tick and tick_pps_gen on tick_clk (50 MHz, MMCM from clk_fpga_0); the hop
# controller, the framer and tick_hop_gpio_sync on sys_cpu_clk (clk_fpga_0, 100 MHz);
# vrt49_time_base_0 on the AD9361 data clock (util_ad9361_divclk, unrelated to both).
# Constraint values are derived from the clocks Vivado resolves on the cells, as vrt49's
# system_vrt49_cdc.tcl does for its Gray counter.  No set_clock_groups: it would override
# vrt49's bounded set_max_delay exceptions.

proc hopcap_a9_cells {pattern what} {
  set cells [get_cells -quiet -hier -filter "NAME =~ $pattern"]
  if {[llength $cells] == 0} {
    error "hopcap adrv9361 CDC: no cells for $what ($pattern)"
  }
  return $cells
}

proc hopcap_a9_d_pins {cells} {
  return [get_pins -of_objects $cells -filter {REF_PIN_NAME == D}]
}

proc hopcap_a9_min_period {cells what} {
  set clks [get_clocks -quiet -of_objects $cells]
  if {[llength $clks] == 0} {
    error "hopcap adrv9361 CDC: no clock resolved for $what"
  }
  return [get_property -min PERIOD $clks]
}

# --- Tick gpio_out (tick_clk) -> tick_hop_gpio_sync (sys_cpu_clk) -------------------------
# gpio_meta is the ASYNC_REG first stage of all four bits (event + slot); cut only it.
set a9_gpio_meta [hopcap_a9_cells {*tick_hop_gpio_sync*gpio_meta_reg*} "tick_hop_gpio_sync first stage"]
if {[llength $a9_gpio_meta] != 4} {
  error "hopcap adrv9361 CDC: expected 4 tick_hop_gpio_sync gpio_meta bits, found [llength $a9_gpio_meta]"
}
set_false_path -to [hopcap_a9_d_pins $a9_gpio_meta]

# --- vrt49_time_base_0 (AD9361 divclk) -------------------------------------------------
# Single-bit first stages: pps (tick_pps_gen, tick_clk), ppssel and secreq (framer
# registers, sys_cpu_clk).  pps_alt_in is a constant, so its synchronizer is optimised away.
foreach stage {pps ppssel secreq} {
  set first [hopcap_a9_cells "*vrt49_time_base_0*${stage}_meta_reg" "time base $stage first stage"]
  if {[llength $first] != 1} {
    error "hopcap adrv9361 CDC: expected one time base ${stage}_meta_reg, found [llength $first]"
  }
  set_false_path -to [hopcap_a9_d_pins $first]
}

set a9_tb_cells [hopcap_a9_cells {*vrt49_time_base_0*pps_sync_reg} "time base pps second stage"]
set a9_divclk_period [hopcap_a9_min_period $a9_tb_cells "vrt49_time_base_0 (divclk)"]
set a9_framer_cells [hopcap_a9_cells {*axi_vrt49_framer_0*tgl_meta_reg} "framer tuple toggle first stage"]
set a9_sys_period [hopcap_a9_min_period $a9_framer_cells "axi_vrt49_framer_0 (sys_cpu_clk)"]

# Quasi-static configuration (nominal sps, margin, immediate-load) from the framer register
# file into the time base's two-stage cfg_* synchronizers: software holds it while the time
# base runs.  Bound it to two divclk periods (as vrt49's ccfmc_lvds target does) instead of
# timing it against an unrelated clock phase.
set a9_cfg_src [get_pins -of_objects \
  [hopcap_a9_cells {*axi_vrt49_framer_0*reg_tb_*_reg*} "framer time-base config registers"] \
  -filter {REF_PIN_NAME == C}]
set a9_cfg_meta [hopcap_a9_cells {*vrt49_time_base_0*cfg_*_meta_reg*} "time base cfg first stages"]
set_max_delay -datapath_only [expr {2.0 * $a9_divclk_period}] \
  -from $a9_cfg_src -to [hopcap_a9_d_pins $a9_cfg_meta]

# cfg_seconds_next is bundled data behind the secreq toggle, sampled on the synchronized
# request edge at least two divclk periods after the toggle arrives: bound every bit to one
# divclk period (vrt49 ccfmc_lvds and ZU11EG targets).
set a9_sec_src [get_pins -of_objects \
  [hopcap_a9_cells {*axi_vrt49_framer_0*tb_seconds_*_reg*} "framer seconds-arm registers"] \
  -filter {REF_PIN_NAME == C}]
set a9_sec_dst [hopcap_a9_cells {*vrt49_time_base_0*seconds_armed_reg*} "time base armed seconds"]
set_max_delay -datapath_only $a9_divclk_period -from $a9_sec_src -to $a9_sec_dst

# The published tuple (divclk) is bundled data qualified by tuple_toggle; it is stable for a
# second around each toggle.  Bound the bus and the toggle into the framer to one
# sys_cpu_clk period.
set a9_tuple_src [get_pins -of_objects [get_cells -hier -filter {
  NAME =~ *vrt49_time_base_0*tuple_*_reg* ||
  NAME =~ *vrt49_time_base_0*lock_state_reg* ||
  NAME =~ *vrt49_time_base_0*missed_count_reg*}] -filter {REF_PIN_NAME == C}]
if {[llength $a9_tuple_src] == 0} {
  error "hopcap adrv9361 CDC: no time base tuple source registers"
}
set a9_loc [hopcap_a9_cells {*axi_vrt49_framer_0*loc_*_reg*} "framer tuple capture registers"]
set_max_delay -datapath_only $a9_sys_period -from $a9_tuple_src -to [hopcap_a9_d_pins $a9_loc]
set a9_toggle_src [get_pins -of_objects \
  [hopcap_a9_cells {*vrt49_time_base_0*tuple_toggle_reg*} "time base tuple toggle"] \
  -filter {REF_PIN_NAME == C}]
set_max_delay -datapath_only $a9_sys_period -from $a9_toggle_src \
  -to [hopcap_a9_d_pins $a9_framer_cells]

puts "hopcap adrv9361 CDC: divclk min period $a9_divclk_period ns, sys_cpu_clk $a9_sys_period ns"
