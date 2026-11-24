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
