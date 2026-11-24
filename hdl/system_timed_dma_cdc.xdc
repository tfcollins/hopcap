# SPDX-License-Identifier: MIT
# hopcap timed-dma profile: CDC constraints for the P3b gates and the RX tagger
# (hdl/system_bd_timed_dma.tcl).  Implementation only (system_project.tcl sets
# USED_IN_SYNTHESIS false): the cells live in out-of-context module-reference IP.
#
# Plain XDC, no Tcl control flow (Vivado drops it: Designutils 20-1307).  No
# set_clock_groups: it would override vrt49's bounded set_max_delay exceptions.
#
# tick_dma_gate: level/mode leave the scheduler (sys_cpu_clk) through a tick-clock
# source register (SRC_REG) and enter separate two-flop synchronizers (ASYNC_REG) in
# the device clock (core_clk_b RX, core_clk_a TX).  Each is a quasi-static single bit
# changed by separate scheduler commands, so only the first stage is cut.
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_dma_gate*level_meta_reg/D}]
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_dma_gate*mode_meta_reg/D}]
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_tx_dma_gate*level_meta_reg/D}]
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_tx_dma_gate*mode_meta_reg/D}]

# tick_stream_timestamp_tag inside tick_rx_tag_adapter: round-trip toggle handshake
# (cdc_req sys_cpu_clk -> core_clk_b, cdc_ack back) and two single-bit controls.
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_tag_adapter*cdc_req_meta_reg/D}]
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_tag_adapter*cdc_ack_meta_reg/D}]
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_tag_adapter*enable_meta_reg/D}]
set_false_path -to [get_pins -hier -filter {NAME =~ *tick_rx_tag_adapter*oneshot_meta_reg/D}]
# The tag payload is loaded in sys_cpu_clk on the cycle cdc_req toggles and sampled in
# core_clk_b two-plus synchronizer stages later.  Bound it to one core_clk_b period
# (245.76 MHz) so it always settles before the toggle is seen.
set_max_delay -datapath_only 4.069 \
  -from [get_cells -hier -filter {NAME =~ *tick_rx_tag_adapter*cdc_payload_reg*}] \
  -to   [get_cells -hier -filter {NAME =~ *tick_rx_tag_adapter*tag_axis_reg*}]
# Tagger diagnostics (core_clk_b registers) read back through the scheduler's
# REG_STREAM_TAG_STATUS[31:24] (sys_cpu_clk AXI read register): opaque status, no
# synchronizer.  Bound to one sys_cpu_clk period rather than cut.
set_max_delay -datapath_only 10.000 \
  -from [get_cells -hier -filter {NAME =~ *tick_rx_tag_adapter* && IS_SEQUENTIAL}] \
  -to   [get_cells -hier -filter {NAME =~ *axi_timed_command_scheduler_0* && IS_SEQUENTIAL}]
