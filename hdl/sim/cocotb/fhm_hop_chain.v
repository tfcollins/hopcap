// SPDX-License-Identifier: MIT
// Simulation-only top: vrt49_fhm_hop_ctrl wired to the vrt49 framer exactly as
// hdl/system_bd_fabric_hop.tcl wires them in the block design (fabric_* nets),
// with the framer at WORDS_PER_BEAT=1 so the testbench sees one sample per beat.
`timescale 1ns/1ps
module fhm_hop_chain #(
    parameter integer WORDS_PER_BEAT = 1
)(
    input  wire        clk,
    input  wire        resetn,
    input  wire [3:0]  scheduler_gpio,
    output wire        stream_enable,
    output wire [2:0]  state_out,
    output wire        fabric_context_accepted,

    // hop controller S_AXI
    input  wire [7:0]  s_axi_awaddr,
    input  wire [2:0]  s_axi_awprot,
    input  wire        s_axi_awvalid,
    output wire        s_axi_awready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wvalid,
    output wire        s_axi_wready,
    output wire [1:0]  s_axi_bresp,
    output wire        s_axi_bvalid,
    input  wire        s_axi_bready,
    input  wire [7:0]  s_axi_araddr,
    input  wire [2:0]  s_axi_arprot,
    input  wire        s_axi_arvalid,
    output wire        s_axi_arready,
    output wire [31:0] s_axi_rdata,
    output wire [1:0]  s_axi_rresp,
    output wire        s_axi_rvalid,
    input  wire        s_axi_rready,

    // framer config strobe port + streams
    input  wire [7:0]  cfg_addr,
    input  wire [31:0] cfg_wdata,
    input  wire        cfg_wstrobe,
    input  wire [7:0]  cfg_raddr,
    output wire [31:0] cfg_rdata,
    input  wire [WORDS_PER_BEAT*32-1:0] s_axis_tdata,
    input  wire        s_axis_tvalid,
    output wire        s_axis_tready,
    output wire [WORDS_PER_BEAT*32-1:0] m_axis_tdata,
    output wire [WORDS_PER_BEAT-1:0]    m_axis_tkeep,
    output wire        m_axis_tvalid,
    input  wire        m_axis_tready,
    output wire        m_axis_tlast
);
    wire        fabric_samples_drained;
    wire        fabric_context_valid;
    wire        fabric_context_ready;
    wire [63:0] fabric_rf_frequency_q44_20;
    wire [31:0] fabric_hop_epoch;

    vrt49_fhm_hop_ctrl hop_ctrl (
        .clk(clk), .resetn(resetn),
        .scheduler_gpio(scheduler_gpio),
        .stream_enable(stream_enable),
        .gate_open(),
        .state_out(state_out),
        .fabric_samples_drained(fabric_samples_drained),
        .fabric_context_valid(fabric_context_valid),
        .fabric_context_ready(fabric_context_ready),
        .fabric_rf_frequency_q44_20(fabric_rf_frequency_q44_20),
        .fabric_hop_epoch(fabric_hop_epoch),
        .fabric_context_accepted(fabric_context_accepted),
        .s_axi_awaddr(s_axi_awaddr), .s_axi_awprot(s_axi_awprot),
        .s_axi_awvalid(s_axi_awvalid), .s_axi_awready(s_axi_awready),
        .s_axi_wdata(s_axi_wdata), .s_axi_wstrb(s_axi_wstrb),
        .s_axi_wvalid(s_axi_wvalid), .s_axi_wready(s_axi_wready),
        .s_axi_bresp(s_axi_bresp), .s_axi_bvalid(s_axi_bvalid), .s_axi_bready(s_axi_bready),
        .s_axi_araddr(s_axi_araddr), .s_axi_arprot(s_axi_arprot),
        .s_axi_arvalid(s_axi_arvalid), .s_axi_arready(s_axi_arready),
        .s_axi_rdata(s_axi_rdata), .s_axi_rresp(s_axi_rresp),
        .s_axi_rvalid(s_axi_rvalid), .s_axi_rready(s_axi_rready)
    );

    axi_vrt49_framer #(.WORDS_PER_BEAT(WORDS_PER_BEAT)) framer (
        .clk(clk), .resetn(resetn),
        .s_axis_tdata(s_axis_tdata), .s_axis_tvalid(s_axis_tvalid), .s_axis_tready(s_axis_tready),
        .m_axis_tdata(m_axis_tdata), .m_axis_tkeep(m_axis_tkeep), .m_axis_tvalid(m_axis_tvalid),
        .m_axis_tready(m_axis_tready), .m_axis_tlast(m_axis_tlast),
        .cfg_addr(cfg_addr), .cfg_wdata(cfg_wdata), .cfg_wstrobe(cfg_wstrobe),
        .cfg_raddr(cfg_raddr), .cfg_rdata(cfg_rdata),
        .ptp_tai_seconds_in(32'd0), .ptp_sample_counter_in(64'd0),
        .tb_tuple_toggle(1'b0), .tb_tuple_seconds(32'd0), .tb_tuple_count(64'd0),
        .tb_tuple_pps_period(32'd0), .tb_tuple_lock_state(2'd0), .tb_tuple_missed_count(8'd0),
        .source_loss_count(32'd0),
        .fabric_sample_enable(stream_enable),
        .fabric_samples_drained(fabric_samples_drained),
        .fabric_context_valid(fabric_context_valid),
        .fabric_context_ready(fabric_context_ready),
        .fabric_rf_frequency_q44_20(fabric_rf_frequency_q44_20),
        .fabric_hop_epoch(fabric_hop_epoch),
        .fabric_context_accepted(fabric_context_accepted),
        .tb_seconds_req(), .tb_seconds_next(), .tb_seconds_immediate(),
        .tb_nominal_sps(), .tb_pps_margin(), .tb_pps_source(),
        .status_pkt_count(), .status_overflow_count()
    );
endmodule
