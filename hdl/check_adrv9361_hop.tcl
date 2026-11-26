# SPDX-License-Identifier: MIT
#
# hopcap adrv9361 profile: post-route gate, sourced by the staged
# adrv9361z7035_vrt49_sfp/system_project.tcl after vrt49's final timing and bus-skew gates
# (the routed impl_1 is open).  Modelled on vrt49's ZU11EG check_pps_cdc.tcl.
#
# Fails the build unless, on the routed design:
#   * every CDC first stage of system_adrv9361_hop_cdc.tcl carries a false path (setup and
#     hold) and its second stage is timed with non-negative slack;
#   * every bounded crossing has a timed path whose requirement is no larger than its bound
#     and whose slack is non-negative;
#   * the three CTRL_IN registers (tick_ctrl_in_iob_reg[2:0], system_top.v) are placed in
#     OLOGIC sites, i.e. packed into the IOBs, so the three pins change together.
# Every result is written to hopcap_adrv9361_post_route.rpt; check_hop_netlist.py
# --profile adrv9361 requires that report and its final "HOPCAP_ADRV9361 ALL PASS" line.

set a9_rpt_name "hopcap_adrv9361_post_route.rpt"
set a9_lines {}
set a9_fail 0

proc a9_note {status what} {
  upvar #0 a9_lines lines a9_fail fail
  lappend lines "HOPCAP_ADRV9361 $status $what"
  if {$status ne "PASS"} {
    set fail 1
  }
  puts "HOPCAP_ADRV9361 $status $what"
}

proc a9_cells {pattern} {
  return [get_cells -quiet -hier -filter "NAME =~ $pattern"]
}

proc a9_pin {cell name} {
  return [get_pins -quiet -of_objects $cell -filter "REF_PIN_NAME == $name"]
}

# First stage cut, second stage timed.
proc a9_sync {what first_pattern second_pattern expect} {
  set firsts [a9_cells $first_pattern]
  set seconds [a9_cells $second_pattern]
  if {[llength $firsts] != $expect || [llength $seconds] != $expect} {
    a9_note FAIL "$what: expected $expect first/second stages, found [llength $firsts]/[llength $seconds]"
    return
  }
  foreach cell [concat $firsts $seconds] {
    if {![get_property ASYNC_REG $cell]} {
      a9_note FAIL "$what: [get_property NAME $cell] lacks ASYNC_REG"
    }
  }
  foreach first $firsts {
    set d [a9_pin $first D]
    set exc [report_exceptions -to $d -no_header -return_string]
    if {![regexp -line {false[ \t]+false[ \t]*$} $exc]} {
      a9_note FAIL "$what: no false path into [get_property NAME $d]"
    } else {
      a9_note PASS "$what: false path into [get_property NAME $d]"
    }
  }
  set paths [get_timing_paths -quiet -from [get_pins -of_objects $firsts -filter {REF_PIN_NAME == Q}] \
    -to [get_pins -of_objects $seconds -filter {REF_PIN_NAME == D}] -max_paths 1]
  if {[llength $paths] != 1 || [get_property SLACK $paths] < 0} {
    a9_note FAIL "$what: second stage untimed or failing setup"
  } else {
    a9_note PASS "$what: second stage timed, slack [get_property SLACK $paths]"
  }
}

# A bounded (max_delay -datapath_only) crossing: timed, requirement within bound, met.
proc a9_bounded {what from to bound} {
  if {[llength $from] == 0 || [llength $to] == 0} {
    a9_note FAIL "$what: endpoints not found"
    return
  }
  set paths [get_timing_paths -quiet -from $from -to $to -max_paths 1 -nworst 1]
  if {[llength $paths] != 1} {
    a9_note FAIL "$what: no timed path (bound missing or cut)"
    return
  }
  set req [get_property REQUIREMENT $paths]
  set slack [get_property SLACK $paths]
  if {$req eq "" || $req > $bound + 0.001} {
    a9_note FAIL "$what: requirement $req ns exceeds bound $bound ns (exception not applied)"
  } elseif {$slack eq "" || $slack < 0} {
    a9_note FAIL "$what: slack $slack ns"
  } else {
    a9_note PASS "$what: requirement $req ns <= $bound ns, slack $slack ns"
  }
}

# --- synchronizer first stages -------------------------------------------------------------
a9_sync "tick_hop_gpio_sync (tick_clk -> sys_cpu_clk)" \
  {*tick_hop_gpio_sync*gpio_meta_reg*} {*tick_hop_gpio_sync*gpio_s1_reg*} 4
foreach stage {pps ppssel secreq} {
  a9_sync "vrt49_time_base_0 $stage" \
    "*vrt49_time_base_0*${stage}_meta_reg" "*vrt49_time_base_0*${stage}_sync_reg" 1
}

# --- bounded crossings -----------------------------------------------------------------
set a9_divclk [get_clocks -quiet -of_objects [a9_cells {*vrt49_time_base_0*pps_sync_reg}]]
set a9_sysclk [get_clocks -quiet -of_objects [a9_cells {*axi_vrt49_framer_0*tgl_meta_reg}]]
if {[llength $a9_divclk] == 0 || [llength $a9_sysclk] == 0} {
  a9_note FAIL "clocks of vrt49_time_base_0 / axi_vrt49_framer_0 not resolved"
} else {
  set div_p [get_property -min PERIOD $a9_divclk]
  set sys_p [get_property -min PERIOD $a9_sysclk]
  a9_bounded "time base config bus (framer reg_tb_* -> cfg_*_meta)" \
    [get_pins -of_objects [a9_cells {*axi_vrt49_framer_0*reg_tb_*_reg*}] -filter {REF_PIN_NAME == C}] \
    [get_pins -of_objects [a9_cells {*vrt49_time_base_0*cfg_*_meta_reg*}] -filter {REF_PIN_NAME == D}] \
    [expr {2.0 * $div_p}]
  a9_bounded "time base seconds-next (framer tb_seconds_* -> seconds_armed)" \
    [get_pins -of_objects [a9_cells {*axi_vrt49_framer_0*tb_seconds_*_reg*}] -filter {REF_PIN_NAME == C}] \
    [a9_cells {*vrt49_time_base_0*seconds_armed_reg*}] \
    $div_p
  a9_bounded "time base tuple bus (tuple_* -> framer loc_*)" \
    [get_pins -of_objects [a9_cells {*vrt49_time_base_0*tuple_*_reg*}] -filter {REF_PIN_NAME == C}] \
    [get_pins -of_objects [a9_cells {*axi_vrt49_framer_0*loc_*_reg*}] -filter {REF_PIN_NAME == D}] \
    $sys_p
  a9_bounded "time base tuple toggle (tuple_toggle -> framer tgl_meta)" \
    [get_pins -of_objects [a9_cells {*vrt49_time_base_0*tuple_toggle_reg*}] -filter {REF_PIN_NAME == C}] \
    [get_pins -of_objects [a9_cells {*axi_vrt49_framer_0*tgl_meta_reg}] -filter {REF_PIN_NAME == D}] \
    $sys_p
}

# --- CTRL_IN[3:1] in IOB output registers --------------------------------------------------
set a9_iob [a9_cells {*tick_ctrl_in_iob_reg*}]
if {[llength $a9_iob] != 3} {
  a9_note FAIL "CTRL_IN: expected 3 tick_ctrl_in_iob_reg cells, found [llength $a9_iob]"
}
foreach cell $a9_iob {
  set loc [get_property LOC $cell]
  set bel [get_property BEL $cell]
  if {[string match -nocase "OLOGIC*" $loc] || [string match -nocase "*OLOGIC*" $bel]} {
    a9_note PASS "CTRL_IN IOB [get_property NAME $cell] LOC=$loc BEL=$bel"
  } else {
    a9_note FAIL "CTRL_IN IOB [get_property NAME $cell] not in an OLOGIC site (LOC=$loc BEL=$bel)"
  }
}

if {$a9_fail} {
  lappend a9_lines "HOPCAP_ADRV9361 FAILED"
} else {
  lappend a9_lines "HOPCAP_ADRV9361 ALL PASS"
}
set a9_fd [open $a9_rpt_name w]
puts $a9_fd [join $a9_lines "\n"]
close $a9_fd
report_cdc -details -file hopcap_adrv9361_cdc.rpt
if {$a9_fail} {
  error "hopcap adrv9361 post-route gate failed; see $a9_rpt_name"
}
puts "hopcap adrv9361 post-route gate: ALL PASS ($a9_rpt_name)"
