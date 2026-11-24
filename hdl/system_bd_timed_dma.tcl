# SPDX-License-Identifier: MIT
#
# hopcap timed-dma profile (libdatax P3b, image candidate A, spike gate G1).
#
# Sourced from the staged adrv9009zu11eg_vrt49_corundum/system_bd.tcl AFTER
# system_bd_vrt49.tcl, Tick's tick_adrv9009zu11eg_adrv2crr_fmc.tcl and
# system_bd_fabric_hop.tcl.  Only `assemble_workspace.py --profile timed-dma` stages and
# sources it.  It inserts, at the wiring the P3b G2/G3 spike validated in simulation
# (tick p3b-spike-gate 3455531, real ADI util_cpack2 / axi_dmac / axi_dacfifo / util_upack2):
#
#   RX  rx_adrv9009_som_tpl_core/adc_valid_0 --> tick_rx_dma_gate --> util_som_rx_cpack/fifo_wr_en
#       (the cpack PIN only; the adc_valid_0 NET keeps feeding vrt49_rx_tap, vrt49_time_base_0
#       and the RoCE capture, so VRT and its time base are not gated)
#       adc_rst | tick_rx_dma_gate/pack_reset --> util_som_rx_cpack/reset
#       (the cpack PIN only; the adc_rst NET keeps feeding vrt49_adc_reset_inv)
#       util_som_rx_cpack/packed_fifo_wr + packed_sync --> tick_rx_tag_adapter (+ the unmodified
#       tick_stream_timestamp_tag) --> axi_adrv9009_som_rx_dma/fifo_wr + sync
#   TX  tx_adrv9009_som_tpl_core/dac_valid_0 --> tick_tx_dma_gate --> util_som_tx_upack/fifo_rd_en
#       util_som_tx_upack/fifo_rd_data_0..7 --> concat --> tick_tx_dma_gate --> slices -->
#       tx_adrv9009_som_tpl_core/dac_data_0..7   (closures mute, start_at holds reads, N=3)
#       The stock axi_adrv9009_som_tx_dma -> axi_tx_fifo nets stay (VRT_DAC_TX_ENABLE=0).
#
# Tick gate bits (stream_gate_en, P3b decision 2): 0 RX level, 1 TX level, 2 RX timed mode,
# 3 TX timed mode, 4..7 spare.  pass = !mode | level, so at reset every gate passes and the
# image streams exactly like the stock design.
#
# Clock crossings (sys_cpu_clk -> core_clk_b / core_clk_a) are constrained by
# system_timed_dma_cdc.xdc.

if {![info exists ad_project_params(VRT_DAC_TX_ENABLE)] || $ad_project_params(VRT_DAC_TX_ENABLE) != 0} {
  error "hopcap timed-dma: VRT_DAC_TX_ENABLE must be 0 (build_bitstream.sh exports it from MANIFEST build_env)"
}
if {[llength [get_bd_cells -quiet vrt49_dac_tx_0]]} {
  error "hopcap timed-dma: vrt49_dac_tx_0 exists; the profile needs the stock TX DMA -> axi_tx_fifo path"
}
foreach cell {axi_timed_command_scheduler_0 util_som_rx_cpack util_som_tx_upack \
              axi_adrv9009_som_rx_dma axi_adrv9009_som_tx_dma axi_tx_fifo vrt49_rx_tap \
              vrt49_adc_reset_inv rx_adrv9009_som_tpl_core tx_adrv9009_som_tpl_core} {
  if {![llength [get_bd_cells -quiet $cell]]} {
    error "hopcap timed-dma: expected block-design cell $cell is missing"
  }
}

# Module-reference RTL (staged by the assembler; Tick's fragment adds the scheduler the same way).
foreach rel {tick_dma_gate/tick_dma_gate.v \
             tick_stream_timestamp_tag/tick_stream_timestamp_tag.v \
             tick_fifo_tag_adapter/tick_fifo_tag_adapter.v} {
  set rtl "$ad_hdl_dir/library/$rel"
  if {![file exists $rtl]} {
    error "hopcap timed-dma: $rtl not staged"
  }
  if {[llength [get_files -quiet $rtl]] == 0} {
    add_files -norecurse $rtl
  }
}
update_compile_order -fileset sources_1

# Tick: 8 stream gates (already the fragment's value; set again so a fragment change cannot
# silently drop gates 2/3) and the in-band timestamp capability bit (P3b decision 8).
set_property -dict [list \
  CONFIG.STREAM_GATE_COUNT {8} \
  CONFIG.HAS_STREAM_TIMESTAMP {1}] [get_bd_cells axi_timed_command_scheduler_0]

proc hopcap_tdma_net_of {pin} {
  set p [get_bd_pins -quiet $pin]
  if {$p eq ""} {
    error "hopcap timed-dma: pin $pin not found"
  }
  return [get_bd_nets -quiet -of_objects $p]
}

# Detach one sink pin from its net without touching the net's other loads.
proc hopcap_tdma_detach {pin} {
  set net [hopcap_tdma_net_of $pin]
  if {$net eq ""} {
    error "hopcap timed-dma: $pin is not connected; the stock wiring changed"
  }
  disconnect_bd_net $net [get_bd_pins $pin]
  return $net
}

proc hopcap_tdma_same_net {a b what} {
  set na [hopcap_tdma_net_of $a]
  set nb [hopcap_tdma_net_of $b]
  if {$na eq "" || $na ne $nb} {
    error "hopcap timed-dma: $what: $a ($na) and $b ($nb) are not on one net"
  }
}

proc hopcap_tdma_width {pin} {
  set p [get_bd_pins -quiet $pin]
  if {$p eq ""} {
    error "hopcap timed-dma: pin $pin not found"
  }
  return [ad_connect_int_width $p]
}

# Reset polarity of the module-reference reset ports (inferred from the names; set explicitly
# so the BD never treats an active-high adc_rst/dac_rst as active-low).
proc hopcap_tdma_active_high {pin} {
  catch {set_property CONFIG.POLARITY ACTIVE_HIGH [get_bd_pins $pin]}
}

# ---------------------------------------------------------------------------------------
# Gate bits from Tick's stream_gate_en (already externalized as tick_stream_gate_en).
foreach {name bit} {tick_gate_rx_level 0 tick_gate_tx_level 1 tick_gate_rx_mode 2 tick_gate_tx_mode 3} {
  ad_ip_instance xlslice $name [list DIN_WIDTH 8 DIN_FROM $bit DIN_TO $bit DOUT_WIDTH 1]
  ad_connect axi_timed_command_scheduler_0/stream_gate_en $name/Din
}
ad_ip_instance xlconstant tick_tdma_zero_1 [list CONST_WIDTH 1 CONST_VAL 0]
ad_ip_instance xlconstant tick_tdma_zero_256 [list CONST_WIDTH 256 CONST_VAL 0]

# ---------------------------------------------------------------------------------------
# RX gate: re-route the cpack fifo_wr_en PIN and the cpack reset PIN.
create_bd_cell -type module -reference tick_dma_gate tick_rx_dma_gate
set_property -dict [list \
  CONFIG.DIRECTION {0} \
  CONFIG.TX_DATA_WIDTH {256} \
  CONFIG.SRC_REG {1} \
  CONFIG.OPEN_DELAY {12} \
  CONFIG.TX_PAUSE_READS {0} \
  CONFIG.TX_START_MUTE {3}] [get_bd_cells tick_rx_dma_gate]
hopcap_tdma_active_high tick_rx_dma_gate/dev_rst
ad_connect sys_cpu_clk tick_rx_dma_gate/tick_clk
ad_connect tick_gate_rx_level/Dout tick_rx_dma_gate/gate_level
ad_connect tick_gate_rx_mode/Dout tick_rx_dma_gate/gate_mode
ad_connect core_clk_b tick_rx_dma_gate/dev_clk
ad_connect rx_adrv9009_som_tpl_core/adc_tpl_core/adc_rst tick_rx_dma_gate/dev_rst
ad_connect tick_tdma_zero_1/dout tick_rx_dma_gate/tx_rd_en_in
ad_connect tick_tdma_zero_256/dout tick_rx_dma_gate/tx_data_in

hopcap_tdma_detach util_som_rx_cpack/fifo_wr_en
ad_connect rx_adrv9009_som_tpl_core/adc_valid_0 tick_rx_dma_gate/rx_valid_in
ad_connect tick_rx_dma_gate/rx_valid_out util_som_rx_cpack/fifo_wr_en

hopcap_tdma_detach util_som_rx_cpack/reset
ad_ip_instance util_vector_logic tick_rx_pack_reset_or [list C_OPERATION or C_SIZE 1]
ad_connect rx_adrv9009_som_tpl_core/adc_tpl_core/adc_rst tick_rx_pack_reset_or/Op1
ad_connect tick_rx_dma_gate/pack_reset tick_rx_pack_reset_or/Op2
ad_connect tick_rx_pack_reset_or/Res util_som_rx_cpack/reset
# Vivado infers the module-reference output pack_reset as an ACTIVE_LOW reset and propagates it
# through the OR, which then mismatches cpack's ACTIVE_HIGH reset (BD 41-238, first G1 attempt).
# Both are active high.
hopcap_tdma_active_high tick_rx_dma_gate/pack_reset
set_property CONFIG.POLARITY ACTIVE_HIGH [get_bd_pins tick_rx_pack_reset_or/Res]

# The VRT taps must still sit on the TPL nets (G2(d)): fail at BD time, not after route.
hopcap_tdma_same_net tick_rx_dma_gate/rx_valid_in vrt49_rx_tap/fifo_wr_en "vrt49 rx tap on TPL adc_valid_0"
hopcap_tdma_same_net tick_rx_dma_gate/rx_valid_in vrt49_time_base_0/sample_valid "vrt49 time base on TPL adc_valid_0"
hopcap_tdma_same_net tick_rx_pack_reset_or/Op1 vrt49_adc_reset_inv/Op1 "adc_rst net"
hopcap_tdma_same_net tick_rx_pack_reset_or/Op1 tick_rx_dma_gate/dev_rst "adc_rst net"

# ---------------------------------------------------------------------------------------
# RX tagger: cpack packed_fifo_wr (interface net) + packed_sync -> adapter -> RX DMA.
set tdma_pk_w [hopcap_tdma_width util_som_rx_cpack/packed_fifo_wr_data]
if {$tdma_pk_w != 128} {
  error "hopcap timed-dma: cpack packed width $tdma_pk_w != 128 (the width spike G3 validated)"
}
set tdma_fifo_wr [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins util_som_rx_cpack/packed_fifo_wr]]
if {$tdma_fifo_wr eq ""} {
  error "hopcap timed-dma: util_som_rx_cpack/packed_fifo_wr is not connected to the RX DMA"
}
delete_bd_objs $tdma_fifo_wr
hopcap_tdma_detach axi_adrv9009_som_rx_dma/sync

create_bd_cell -type module -reference tick_fifo_tag_adapter tick_rx_tag_adapter
set_property -dict [list \
  CONFIG.DATA_WIDTH $tdma_pk_w \
  CONFIG.TAG_GATE_ID {0} \
  CONFIG.ENABLE_TAGGER {1} \
  CONFIG.DATA_DELAY {0} \
  CONFIG.REARM_ON_PASS {1}] [get_bd_cells tick_rx_tag_adapter]
hopcap_tdma_active_high tick_rx_tag_adapter/rst
ad_connect core_clk_b tick_rx_tag_adapter/clk
ad_connect rx_adrv9009_som_tpl_core/adc_tpl_core/adc_rst tick_rx_tag_adapter/rst
ad_connect util_som_rx_cpack/packed_fifo_wr_en   tick_rx_tag_adapter/s_fifo_wr_en
ad_connect util_som_rx_cpack/packed_fifo_wr_data tick_rx_tag_adapter/s_fifo_wr_data
ad_connect util_som_rx_cpack/packed_sync         tick_rx_tag_adapter/s_fifo_wr_sync
ad_connect tick_rx_tag_adapter/s_fifo_wr_overflow util_som_rx_cpack/packed_fifo_wr_overflow
ad_connect tick_rx_tag_adapter/m_fifo_wr_en   axi_adrv9009_som_rx_dma/fifo_wr_en
ad_connect tick_rx_tag_adapter/m_fifo_wr_data axi_adrv9009_som_rx_dma/fifo_wr_din
ad_connect tick_rx_tag_adapter/m_fifo_wr_sync axi_adrv9009_som_rx_dma/sync
ad_connect axi_adrv9009_som_rx_dma/fifo_wr_overflow tick_rx_tag_adapter/m_fifo_wr_overflow
ad_connect tick_rx_dma_gate/pass_dev tick_rx_tag_adapter/pass_dev
ad_connect tick_rx_dma_gate/mode_dev tick_rx_tag_adapter/mode_dev

ad_connect sys_cpu_clk tick_rx_tag_adapter/tag_clk
foreach f {ticks seq target command gate count late valid} {
  ad_connect axi_timed_command_scheduler_0/capture_tag_$f tick_rx_tag_adapter/tag_$f
}
ad_connect axi_timed_command_scheduler_0/stream_tag_oneshot tick_rx_tag_adapter/tag_oneshot
ad_ip_instance xlslice tick_tag_en_slice [list DIN_WIDTH 8 DIN_FROM 0 DIN_TO 0 DOUT_WIDTH 1]
ad_connect axi_timed_command_scheduler_0/stream_tag_en tick_tag_en_slice/Din
ad_connect tick_tag_en_slice/Dout tick_rx_tag_adapter/tag_enable
# Must equal the scheduler's TIME_TICK_HZ (sys_cpu_clk, 100 MHz).
ad_ip_instance xlconstant tick_tag_tick_hz [list CONST_WIDTH 32 CONST_VAL 100000000]
ad_connect tick_tag_tick_hz/dout tick_rx_tag_adapter/tag_tick_hz
# Tagger diagnostics -> REG_STREAM_TAG_STATUS[31:24].
ad_connect tick_rx_tag_adapter/dbg_status axi_timed_command_scheduler_0/stream_tag_probe

# ---------------------------------------------------------------------------------------
# TX gate: upack fifo_rd_en PIN and the upack -> TPL dac_data_i pins.
create_bd_cell -type module -reference tick_dma_gate tick_tx_dma_gate
set_property -dict [list \
  CONFIG.DIRECTION {1} \
  CONFIG.TX_DATA_WIDTH {256} \
  CONFIG.SRC_REG {1} \
  CONFIG.OPEN_DELAY {0} \
  CONFIG.TX_PAUSE_READS {0} \
  CONFIG.TX_START_MUTE {3}] [get_bd_cells tick_tx_dma_gate]
hopcap_tdma_active_high tick_tx_dma_gate/dev_rst
ad_connect sys_cpu_clk tick_tx_dma_gate/tick_clk
ad_connect tick_gate_tx_level/Dout tick_tx_dma_gate/gate_level
ad_connect tick_gate_tx_mode/Dout tick_tx_dma_gate/gate_mode
ad_connect core_clk_a tick_tx_dma_gate/dev_clk
ad_connect tx_adrv9009_som_tpl_core/dac_tpl_core/dac_rst tick_tx_dma_gate/dev_rst
ad_connect tick_tdma_zero_1/dout tick_tx_dma_gate/rx_valid_in

hopcap_tdma_detach util_som_tx_upack/fifo_rd_en
ad_connect tx_adrv9009_som_tpl_core/dac_valid_0 tick_tx_dma_gate/tx_rd_en_in
ad_connect tick_tx_dma_gate/tx_rd_en_out util_som_tx_upack/fifo_rd_en

set tdma_tx_ch [llength [get_bd_pins -quiet util_som_tx_upack/fifo_rd_data_*]]
set tdma_tx_w [hopcap_tdma_width util_som_tx_upack/fifo_rd_data_0]
if {$tdma_tx_ch * $tdma_tx_w != 256} {
  error "hopcap timed-dma: upack is $tdma_tx_ch x $tdma_tx_w bit, not the 256 bit the TX gate was validated at"
}
set tdma_concat_cfg [list NUM_PORTS $tdma_tx_ch]
for {set i 0} {$i < $tdma_tx_ch} {incr i} {
  lappend tdma_concat_cfg IN${i}_WIDTH $tdma_tx_w
}
ad_ip_instance xlconcat tick_tx_gate_data_concat $tdma_concat_cfg
for {set i 0} {$i < $tdma_tx_ch} {incr i} {
  # In0 is the LSB: channel i occupies tx_data[i*w +: w], as the spike's TX bench packs it.
  # Detach the upack (leaf) pin; the TPL hierarchy pin keeps its net and gets the slice.
  hopcap_tdma_detach util_som_tx_upack/fifo_rd_data_$i
  ad_connect util_som_tx_upack/fifo_rd_data_$i tick_tx_gate_data_concat/In$i
  ad_ip_instance xlslice tick_tx_gate_data_slice_$i [list \
    DIN_WIDTH 256 DIN_FROM [expr {$i * $tdma_tx_w + $tdma_tx_w - 1}] \
    DIN_TO [expr {$i * $tdma_tx_w}] DOUT_WIDTH $tdma_tx_w]
  ad_connect tick_tx_dma_gate/tx_data_out tick_tx_gate_data_slice_$i/Din
  ad_connect tick_tx_gate_data_slice_$i/Dout tx_adrv9009_som_tpl_core/dac_data_$i
}
ad_connect tick_tx_gate_data_concat/dout tick_tx_dma_gate/tx_data_in

# Stock TX DMA -> axi_tx_fifo, untouched by vrt49 at VRT_DAC_TX_ENABLE=0.
foreach {src dst} {m_axis_data dma_data m_axis_valid dma_valid m_axis_ready dma_ready \
                   m_axis_last dma_xfer_last m_axis_xfer_req dma_xfer_req} {
  hopcap_tdma_same_net axi_adrv9009_som_tx_dma/$src axi_tx_fifo/$dst "stock TX DMA -> axi_tx_fifo"
}

validate_bd_design
puts "hopcap timed-dma: tick_rx_dma_gate (OPEN_DELAY 12, pack_reset) on util_som_rx_cpack fifo_wr_en/reset pins; tick_rx_tag_adapter (${tdma_pk_w} bit) between cpack and axi_adrv9009_som_rx_dma"
puts "hopcap timed-dma: tick_tx_dma_gate (${tdma_tx_ch}x${tdma_tx_w} bit, mute, TX_START_MUTE 3) between util_som_tx_upack and tx_adrv9009_som_tpl_core; stock TX DMA -> axi_tx_fifo"
puts "hopcap timed-dma: gate bits stream_gate_en\[0\] RX level, \[1\] TX level, \[2\] RX mode, \[3\] TX mode; tagger gate 0, HAS_STREAM_TIMESTAMP 1"
