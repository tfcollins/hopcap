# SPDX-License-Identifier: MIT
#
# hopcap adrv9361 profile: the hop composite on the ADRV9361-Z7035 (vrt49
# adrv9361z7035_vrt49_sfp, 1G SFP).  Design: docs/superpowers/specs/
# 2026-11-25-adrv9361z7035-hop-design.md.
#
# Sourced from the staged adrv9361z7035_vrt49_sfp/system_bd.tcl after vrt49's block-design
# body and BEFORE its final validate_bd_design (assemble_workspace.py --profile adrv9361
# inserts the source line).  The assembler also removes vrt49's own Tick (tick_vendor IP,
# 100 MHz) + HOP1 (vrt49_ad9361_hop_ctrl) block from that body: neither IP is in this
# project's LIB_DEPS, so their ad_ip_instance lines could not run.  The deletions below
# are kept for a body that still carries them.
#
#   Tick  third_party/tick's Z7035 fragment: module reference at 0x44AA0000, 50 MHz
#         tick_clk from FCLK0 (clk_wiz) behind an async AXI-Lite converter, IRQ ps-14,
#         BD ports tick_clk / tick_ctrl_in[2:0] (system_top.v registers CTRL_IN[2:0] in IOBs).
#         Tick at 50 MHz is required: with retained-slot repeats the scheduler misses
#         100 MHz on the Z7035 -2L (tick docs/adrv9361z7035-tick.md).
#   HOP2  vrt49_fhm_hop_ctrl_0 at 0x44AB0000 on sys_cpu_clk (100 MHz).  It gates and stamps;
#         it never drives the radio.  scheduler_gpio = {gpio_out[2:0], gpio_out[3]}
#         (bit 0 = event, bits 3:1 = slot = the CTRL_IN code) through tick_gpio_sync
#         (slot two flops, event three) into sys_cpu_clk.
#   PPS   tick_pps_gen_0 on tick_clk: one TICK_HZ-cycle second, 100 ms pulse, into Tick's
#         pps_in and vrt49_time_base_0's pps_in (VRT time = Tick time; no GPS).
#   VRT   vrt49_time_base_0 on the AD9361 data clock (util_ad9361_divclk), sample_valid =
#         the tap's packed_fifo_wr_en; framer tb_* / tb_tuple_* wired as on the ZU11EG.
#         CDC: system_adrv9361_hop_cdc.tcl, post-route gate check_adrv9361_hop.tcl.
#   Link  vrt49_link_throttle FORWARD_EVERY_N 1: every frame is forwarded (15.36 MSPS, one
#         RX channel, ~0.5 Gb/s on the 1G link); hop stamps need consecutive packets.

set hop_ctrl_base 0x44AB0000
set hopcap_tick_hz 50000000

foreach cell {axi_vrt49_framer_0 vrt49_link_throttle vrt49_rx_tap util_ad9361_adc_pack \
              util_ad9361_divclk util_ad9361_divclk_reset} {
  if {![llength [get_bd_cells -quiet $cell]]} {
    error "hopcap adrv9361: expected block-design cell $cell is missing"
  }
}

proc hopcap_a9_net_of {pin} {
  set p [get_bd_pins -quiet $pin]
  if {$p eq ""} {
    error "hopcap adrv9361: pin $pin not found"
  }
  return [get_bd_nets -quiet -of_objects $p]
}

# Detach one sink pin from its net without touching the net's other loads.
proc hopcap_a9_detach {pin} {
  set net [hopcap_a9_net_of $pin]
  if {$net eq ""} {
    error "hopcap adrv9361: $pin is not connected; vrt49's wiring changed"
  }
  disconnect_bd_net $net [get_bd_pins $pin]
}

proc hopcap_a9_same_net {a b what} {
  set na [hopcap_a9_net_of $a]
  set nb [hopcap_a9_net_of $b]
  if {$na eq "" || $na ne $nb} {
    error "hopcap adrv9361: $what: $a ($na) and $b ($nb) are not on one net"
  }
}

# ---------------------------------------------------------------------------------------
# vrt49's Tick + HOP1 and the fastlock port (removed by the assembler; delete if present).
foreach cell {tick_scheduler_0 vrt49_hop_ctrl_0} {
  if {[llength [get_bd_cells -quiet $cell]]} {
    puts "hopcap adrv9361: deleting vrt49's $cell"
    delete_bd_objs [get_bd_cells $cell]
  }
}
if {[llength [get_bd_ports -quiet ad9361_fastlock_select]]} {
  set fl_net [get_bd_nets -quiet -of_objects [get_bd_ports ad9361_fastlock_select]]
  if {$fl_net ne ""} {
    delete_bd_objs $fl_net
  }
  delete_bd_objs [get_bd_ports ad9361_fastlock_select]
}
foreach cell {tick_scheduler_0 vrt49_hop_ctrl_0 axi_timed_command_scheduler_0} {
  if {[llength [get_bd_cells -quiet $cell]]} {
    error "hopcap adrv9361: $cell still exists before Tick's fragment runs"
  }
}

# Module-reference RTL (staged by the assembler).  A module reference must be in the project
# when create_bd_cell runs; the BD script runs inside adi_project_create, before
# adi_project_files, so add it here (Tick's fragment does the same for the scheduler).
foreach rel {tick_gpio_sync/tick_gpio_sync.v} {
  set rtl "$ad_hdl_dir/library/$rel"
  if {![file exists $rtl]} {
    error "hopcap adrv9361: $rtl not staged"
  }
  if {[llength [get_files -quiet $rtl]] == 0} {
    add_files -norecurse $rtl
  }
}
update_compile_order -fileset sources_1

# ---------------------------------------------------------------------------------------
# Tick, from third_party/tick (the revision whose driver hopcap builds).
source $ad_hdl_dir/projects/adrv9361z7035/ccfmc_lvds/tick_adrv9361z7035_ccfmc_lvds.tcl

set a9_tick_hz [get_property CONFIG.TIME_TICK_HZ [get_bd_cells axi_timed_command_scheduler_0]]
if {$a9_tick_hz != $hopcap_tick_hz} {
  error "hopcap adrv9361: Tick TIME_TICK_HZ is $a9_tick_hz, expected $hopcap_tick_hz"
}
foreach obj {tick_clkgen tick_timebase_reset tick_pps_tieoff} {
  if {![llength [get_bd_cells -quiet $obj]]} {
    error "hopcap adrv9361: Tick's fragment did not create $obj"
  }
}
foreach port {tick_clk tick_ctrl_in} {
  if {![llength [get_bd_ports -quiet $port]]} {
    error "hopcap adrv9361: Tick's fragment did not create BD port $port"
  }
}

# ---------------------------------------------------------------------------------------
# HOP2: vrt49_fhm_hop_ctrl_0 at 0x44AB0000 (HOP1's aperture) on sys_cpu_clk.
ad_ip_instance vrt49_fhm_hop_ctrl vrt49_fhm_hop_ctrl_0 [list \
  DEFAULT_SETTLE_TICKS 2000 \
  DEFAULT_CAPTURE_TICKS 0 \
  DEFAULT_IDLE_OPEN 1]
ad_connect sys_cpu_clk    vrt49_fhm_hop_ctrl_0/clk
ad_connect sys_cpu_resetn vrt49_fhm_hop_ctrl_0/resetn

# Reorder Tick's gpio_out for the controller: AD9361 bit map is gpio_out[2:0] = slot (the
# CTRL_IN code) and gpio_out[3] = event, while the controller wants bit 0 = event and bits
# 3:1 = slot.  xlconcat In0 is the LSB.
ad_ip_instance xlslice tick_hop_event_slice [list \
  DIN_WIDTH 4 DIN_FROM 3 DIN_TO 3 DOUT_WIDTH 1]
ad_ip_instance xlslice tick_hop_slot_slice [list \
  DIN_WIDTH 4 DIN_FROM 2 DIN_TO 0 DOUT_WIDTH 3]
ad_connect axi_timed_command_scheduler_0/gpio_out tick_hop_event_slice/Din
ad_connect axi_timed_command_scheduler_0/gpio_out tick_hop_slot_slice/Din
ad_ip_instance xlconcat tick_hop_gpio_concat [list \
  NUM_PORTS 2 IN0_WIDTH 1 IN1_WIDTH 3]
ad_connect tick_hop_event_slice/Dout tick_hop_gpio_concat/In0
ad_connect tick_hop_slot_slice/Dout  tick_hop_gpio_concat/In1

# Tick (tick_clk, 50 MHz) -> controller (sys_cpu_clk, 100 MHz): slot bits two flops, the
# event three, so the slot is settled when the event rises.  First stage cut by
# system_adrv9361_hop_cdc.tcl.
create_bd_cell -type module -reference tick_gpio_sync tick_hop_gpio_sync
ad_connect sys_cpu_clk tick_hop_gpio_sync/clk
ad_connect tick_hop_gpio_concat/dout tick_hop_gpio_sync/gpio_async
ad_connect tick_hop_gpio_sync/gpio_sync vrt49_fhm_hop_ctrl_0/scheduler_gpio

# The seven framer fabric nets HOP1 used (now unconnected: the assembler removed HOP1).
foreach pin {fabric_sample_enable fabric_context_valid fabric_rf_frequency_q44_20 \
             fabric_hop_epoch fabric_samples_drained fabric_context_ready \
             fabric_context_accepted} {
  set net [hopcap_a9_net_of axi_vrt49_framer_0/$pin]
  if {$net ne ""} {
    disconnect_bd_net $net [get_bd_pins axi_vrt49_framer_0/$pin]
  }
}
ad_connect vrt49_fhm_hop_ctrl_0/stream_enable              axi_vrt49_framer_0/fabric_sample_enable
ad_connect axi_vrt49_framer_0/fabric_samples_drained       vrt49_fhm_hop_ctrl_0/fabric_samples_drained
ad_connect vrt49_fhm_hop_ctrl_0/fabric_context_valid       axi_vrt49_framer_0/fabric_context_valid
ad_connect axi_vrt49_framer_0/fabric_context_ready         vrt49_fhm_hop_ctrl_0/fabric_context_ready
ad_connect vrt49_fhm_hop_ctrl_0/fabric_rf_frequency_q44_20 axi_vrt49_framer_0/fabric_rf_frequency_q44_20
ad_connect vrt49_fhm_hop_ctrl_0/fabric_hop_epoch           axi_vrt49_framer_0/fabric_hop_epoch
ad_connect axi_vrt49_framer_0/fabric_context_accepted      vrt49_fhm_hop_ctrl_0/fabric_context_accepted

ad_cpu_interconnect $hop_ctrl_base vrt49_fhm_hop_ctrl_0

# ---------------------------------------------------------------------------------------
# Internal 1PPS on the tick clock -> Tick pps_in (same clock) and the VRT time base.
set tick_pps_net [get_bd_nets -quiet -of_objects [get_bd_pins axi_timed_command_scheduler_0/pps_in]]
if {$tick_pps_net ne ""} {
  delete_bd_objs $tick_pps_net
}
delete_bd_objs [get_bd_cells tick_pps_tieoff]

ad_ip_instance tick_pps_gen tick_pps_gen_0 [list \
  TICK_HZ $hopcap_tick_hz \
  PULSE_TICKS [expr {$hopcap_tick_hz / 10}]]
ad_connect tick_clkgen/clk_out1 tick_pps_gen_0/clk
ad_connect tick_timebase_reset/peripheral_aresetn tick_pps_gen_0/resetn
ad_connect tick_pps_gen_0/pps_out axi_timed_command_scheduler_0/pps_in
hopcap_a9_same_net tick_pps_gen_0/clk axi_timed_command_scheduler_0/s_axi_aclk "tick_pps_gen on Tick's clock"

# ---------------------------------------------------------------------------------------
# VRT time base on the AD9361 data clock, counting the beats the tap forwards.  vrt49 ties
# the framer's tuple inputs to shared constants (which also feed the PTP inputs): detach
# only our pins, then drop constants left without loads.
foreach pin {tb_tuple_toggle tb_tuple_seconds tb_tuple_count tb_tuple_pps_period \
             tb_tuple_lock_state tb_tuple_missed_count} {
  hopcap_a9_detach axi_vrt49_framer_0/$pin
}
foreach cell {vrt49_zero_1 vrt49_zero_2 vrt49_zero_8} {
  if {![llength [get_bd_cells -quiet $cell]]} {
    continue
  }
  set dout_net [get_bd_nets -quiet -of_objects [get_bd_pins $cell/dout]]
  if {$dout_net eq "" || [llength [get_bd_pins -quiet -of_objects $dout_net]] <= 1} {
    delete_bd_objs [get_bd_cells $cell]
  }
}

ad_ip_instance vrt49_time_base vrt49_time_base_0
ad_connect util_ad9361_divclk/clk_out vrt49_time_base_0/divclk
ad_connect util_ad9361_divclk_reset/peripheral_aresetn vrt49_time_base_0/divclk_resetn
ad_connect tick_pps_gen_0/pps_out vrt49_time_base_0/pps_in
ad_ip_instance xlconstant hopcap_tb_pps_alt_tieoff [list CONST_WIDTH 1 CONST_VAL 0]
ad_connect hopcap_tb_pps_alt_tieoff/dout vrt49_time_base_0/pps_alt_in
ad_connect util_ad9361_adc_pack/packed_fifo_wr_en vrt49_time_base_0/sample_valid
foreach sig {tuple_toggle tuple_seconds tuple_count tuple_pps_period tuple_lock_state tuple_missed_count} {
  ad_connect vrt49_time_base_0/$sig axi_vrt49_framer_0/tb_$sig
}
ad_connect axi_vrt49_framer_0/tb_seconds_req       vrt49_time_base_0/cfg_seconds_req
ad_connect axi_vrt49_framer_0/tb_seconds_next      vrt49_time_base_0/cfg_seconds_next
ad_connect axi_vrt49_framer_0/tb_seconds_immediate vrt49_time_base_0/cfg_seconds_immediate
ad_connect axi_vrt49_framer_0/tb_nominal_sps       vrt49_time_base_0/cfg_nominal_sps
ad_connect axi_vrt49_framer_0/tb_pps_margin        vrt49_time_base_0/cfg_pps_margin
ad_connect axi_vrt49_framer_0/tb_pps_source        vrt49_time_base_0/cfg_pps_source
# The time base and the tap must count the same beats on the same clock.
hopcap_a9_same_net vrt49_time_base_0/sample_valid vrt49_rx_tap/sample_valid "time base counts the tap's beats"
hopcap_a9_same_net vrt49_time_base_0/divclk vrt49_rx_tap/clk "time base on the tap's clock"

# ---------------------------------------------------------------------------------------
# No frame throttling: hop stamps need consecutive packets (spec decision 5).
ad_ip_parameter vrt49_link_throttle CONFIG.FORWARD_EVERY_N 1

puts "hopcap adrv9361: Tick (third_party/tick) at 0x44AA0000, $hopcap_tick_hz Hz, IRQ ps-14; CTRL_IN\[2:0\] = gpio_out\[2:0\]"
puts "hopcap adrv9361: vrt49_fhm_hop_ctrl_0 at $hop_ctrl_base on sys_cpu_clk, scheduler_gpio = {gpio_out\[2:0\], gpio_out\[3\]} via tick_hop_gpio_sync"
puts "hopcap adrv9361: tick_pps_gen_0 -> Tick pps_in + vrt49_time_base_0 pps_in; time base on util_ad9361_divclk, sample_valid = packed_fifo_wr_en"
puts "hopcap adrv9361: vrt49_link_throttle FORWARD_EVERY_N 1"
