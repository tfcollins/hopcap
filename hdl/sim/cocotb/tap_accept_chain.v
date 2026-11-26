// SPDX-License-Identifier: MIT
// Test harness: vrt49_sample_tap (DECIMATION 1, as on the adrv9361 profile) + tap_accept.
`timescale 1ns/1ps
module tap_accept_chain (
    input  wire        clk,
    input  wire        resetn,
    input  wire        sample_valid,
    input  wire [31:0] sample_data,
    input  wire        m_axis_tready,
    output wire        m_axis_tvalid,
    output wire [31:0] m_axis_tdata,
    output wire [31:0] forwarded_sample_count,
    output wire [31:0] dropped_sample_count,
    output wire        accepted
);
    vrt49_sample_tap #(.DATA_WIDTH(32), .DECIMATION(1)) i_tap (
        .clk(clk), .resetn(resetn), .sample_data(sample_data), .sample_valid(sample_valid),
        .m_axis_tdata(m_axis_tdata), .m_axis_tvalid(m_axis_tvalid), .m_axis_tready(m_axis_tready),
        .forwarded_sample_count(forwarded_sample_count),
        .dropped_sample_count(dropped_sample_count));
    tap_accept i_accept (
        .clk(clk), .resetn(resetn), .sample_valid(sample_valid),
        .dropped_sample_count(dropped_sample_count), .accepted(accepted));
endmodule
