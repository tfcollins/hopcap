# SPDX-License-Identifier: MIT
# hopcap timed-dma profile: CDC constraints for the P3b gates, the RX tagger, Tick's pps_in
# and the hop controller's GPIO (hdl/system_bd_timed_dma.tcl).  Implementation only
# (system_project.tcl sets USED_IN_SYNTHESIS false): the cells live in out-of-context
# module-reference IP.
#
# Plain XDC, no Tcl control flow (Vivado drops it: Designutils 20-1307).  No
# set_clock_groups: it would override vrt49's bounded set_max_delay exceptions.
#
# Clocking (libdatax P3b option 1, datax_sdk_sdr#40): Tick runs on core_clk_a (HMC7044 ch6,
# 122.88 MHz).  The TX gate (tick_clk = dev_clk = core_clk_a) is same-clock, so its
# synchronizer inputs are timed normally and carry no exception here.  The RX gate and the
# tagger cross core_clk_a -> core_clk_b (ch7, 245.76 MHz, same PLL2): frequency-locked, but
# Vivado sees two unrelated primary clocks, so the synchronizers still get their cuts.

# core_clk_a period.  ADI's adrv9009zu11eg_constr.xdc declares tx_dev_clk on core_clk_a_p at a
# generic 4.00 ns (250 MHz).  On this board's profile HMC7044 ch6 divides PLL2 2949.12 MHz by
# 24: 122.88 MHz (build_boot_artifacts.sh asserts both numbers in the DTB).  Tick's 64-bit
# compare paths need the real period (they miss 4.00 ns by ~2.8 ns and fit 8.138 ns), so
# redefine the clock under the same name.  This file is read after ADI's (system_project.tcl
# file order); Vivado warns once (Constraints 18-619, "overwriting the previous clock").
# build_bitstream.sh records the routed period and verify_evidence.py requires 8.138.  The
# two sysref_a input delays are repeated so they survive the redefinition (checked on the
# routed checkpoint: inter-clock paths and unconstrained-input counts unchanged).  Any design that moves Tick onto core_clk_a
# must constrain the real period the same way.
create_clock -name tx_dev_clk -period 8.138 [get_ports core_clk_a_p]
set_input_delay -clock tx_dev_clk -max 4 [get_ports sysref_a_p]
set_input_delay -clock tx_dev_clk -min 4 [get_ports sysref_a_p]

# RX gate: level/mode leave the scheduler (core_clk_a) through a tick-clock source register
# (SRC_REG) and enter separate two-flop synchronizers (ASYNC_REG) in core_clk_b.  Each is a
# quasi-static single bit changed by separate scheduler commands, so only the first stage is
# cut.
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_dma_gate*level_meta_reg/D}]
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_dma_gate*mode_meta_reg/D}]

# tick_stream_timestamp_tag inside tick_rx_tag_adapter: round-trip toggle handshake
# (cdc_req core_clk_a -> core_clk_b, cdc_ack back) and two single-bit controls.
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_tag_adapter*cdc_req_meta_reg/D}]
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_tag_adapter*cdc_ack_meta_reg/D}]
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_tag_adapter*enable_meta_reg/D}]
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_tag_adapter*oneshot_meta_reg/D}]
# The tag payload is loaded in core_clk_a on the cycle cdc_req toggles and sampled in
# core_clk_b two-plus synchronizer stages later.  Bound it to one core_clk_b period
# (245.76 MHz) so it always settles before the toggle is seen.
set_max_delay -datapath_only 4.069 \
  -from [get_cells -hier -filter {NAME =~ *tick_rx_tag_adapter*cdc_payload_reg*}] \
  -to   [get_cells -hier -filter {NAME =~ *tick_rx_tag_adapter*tag_axis_reg*}]
# Tagger diagnostics (core_clk_b registers) read back through the scheduler's
# REG_STREAM_TAG_STATUS[31:24] (core_clk_a AXI read register): opaque status, no
# synchronizer.  Bound to one core_clk_a period rather than cut.
set_max_delay -datapath_only 8.138 \
  -from [get_cells -hier -filter {NAME =~ *tick_rx_tag_adapter* && IS_SEQUENTIAL}] \
  -to   [get_cells -hier -filter {NAME =~ *axi_timed_command_scheduler_0* && IS_SEQUENTIAL}]

# Tick pps_in: tick_pps_select_0's pps_out (sys_cpu_clk, a 100 ms level) enters the
# scheduler's own two-flop synchronizer (pps_meta -> pps_sync) in core_clk_a.
set_false_path -to [get_pins -hier -filter {NAME =~ *axi_timed_command_scheduler_0*pps_meta_reg/D}]

# Hop controller GPIO: Tick's gpio_out (core_clk_a) into tick_hop_gpio_sync (sys_cpu_clk).
# gpio_meta is the ASYNC_REG first stage for all four bits; the event bit gets one more
# stage than the slot bits, so the slot is settled when the event rises.
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_hop_gpio_sync*gpio_meta_reg*/D}]
