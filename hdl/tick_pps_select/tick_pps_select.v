// SPDX-License-Identifier: MIT
//
// tick_pps_select: choose the 1PPS that drives Tick's pps_in.
//
// Two asynchronous PPS candidates arrive on clk (Tick's sys_cpu_clk, 100 MHz):
//   ext_pps_async  an external 1PPS pin (PMOD0_D4 on hopcap) that may be unconnected,
//                  floating or noisy;
//   int_pps_async  Corundum's free-running PTP PPS (ptp_pps_str, a ~0.54 s high level in the
//                  PTP clock domain), always present once the NIC core runs.
//
// Both are double-synchronised and their rising edges detected continuously, whichever is
// selected.  The external input is *qualified*:
//
//   ext_alive = the last GOOD_PERIODS consecutive external periods (rising edge to rising
//               edge) were each within [PERIOD_MIN, PERIOD_MAX] clk cycles, AND an external
//               rising edge was seen within the last ALIVE_TIMEOUT cycles.
//
// One out-of-range period (glitch, chatter, a floating pin picking up noise) or a timeout
// clears the count, so external must again show GOOD_PERIODS good periods before it is chosen.
//
// sel_ext is the source actually applied (1 = external).  It follows ext_alive, but only
// changes in a quiet cycle: both synchronised inputs low in this and the previous sample, so
// no edge is in progress on either.  If no such cycle occurs within PERIOD_MAX cycles of the
// request (an external pin stuck high after failing, or a wide-duty external in anti-phase
// with Corundum's 54 % level) it changes in the first cycle in which the *target* input alone
// is low in both samples.  Reset selects internal.
//
// pps_out is regenerated, not muxed: an accepted rising edge of the selected input starts a
// PULSE_CYCLES-wide pulse, with the same fixed latency for either source.  Guarantee:
//
//   * every rising edge of pps_out is caused by a rising edge of the then-selected input, so
//     a switch never manufactures an edge (both detectors always run, so an input that is
//     already high when it becomes selected shows no rising edge until it next goes low and
//     high again);
//   * consecutive pps_out rising edges are at least PERIOD_MIN cycles apart: a selected edge
//     closer than that to the previous output edge is dropped.  This absorbs the phase step
//     at a switch and any glitch on a selected external, so Tick never sees a spurious edge;
//   * at most one input edge is dropped per switch (the next one is a full second later);
//   * after reset no edge is emitted until an input has been seen low and then rises.
//
// Requires PULSE_CYCLES < PERIOD_MIN <= PERIOD_MAX < ALIVE_TIMEOUT for the intended
// behaviour (defaults: 0.1 s, 0.9 s, 1.1 s, 1.25 s at 100 MHz).
`timescale 1ns/1ps
module tick_pps_select #(
    parameter integer ALIVE_TIMEOUT = 125000000,
    parameter integer PERIOD_MIN = 90000000,
    parameter integer PERIOD_MAX = 110000000,
    parameter integer GOOD_PERIODS = 2,
    parameter integer PULSE_CYCLES = 10000000
)(
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_RESET resetn" *)
    input wire clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 resetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input wire resetn,
    input wire ext_pps_async,
    input wire int_pps_async,
    output reg pps_out,
    output reg sel_ext,
    output reg ext_alive
);

    localparam integer CNT_MAX = (ALIVE_TIMEOUT > PERIOD_MAX) ? ALIVE_TIMEOUT : PERIOD_MAX;
    localparam integer CW = $clog2(CNT_MAX + 2);
    localparam integer GW = $clog2(GOOD_PERIODS + 1);
    localparam integer SPACING_I = PERIOD_MIN - 1;
    localparam integer PULSE_I = PULSE_CYCLES - 1;
    localparam [CW-1:0] SAT = CNT_MAX[CW-1:0];
    localparam [CW-1:0] P_MIN = PERIOD_MIN[CW-1:0];
    localparam [CW-1:0] P_MAX = PERIOD_MAX[CW-1:0];
    localparam [CW-1:0] TIMEOUT = ALIVE_TIMEOUT[CW-1:0];
    localparam [CW-1:0] SPACING_SAT = SPACING_I[CW-1:0];
    localparam [CW-1:0] PULSE_LAST = PULSE_I[CW-1:0];
    localparam [GW-1:0] GOOD_N = GOOD_PERIODS[GW-1:0];
    localparam [CW-1:0] ONE = {{(CW-1){1'b0}}, 1'b1};
    localparam [GW-1:0] GONE = {{(GW-1){1'b0}}, 1'b1};

    // Two-flop synchronisers plus a third stage for edge detection.  Reset to 1 so an input
    // that is high at reset release shows no rising edge.  system_tick_pps_cdc.xdc cuts the
    // asynchronous path into *_meta_reg/D.
    (* ASYNC_REG = "TRUE" *) reg ext_meta;
    (* ASYNC_REG = "TRUE" *) reg ext_sync;
    reg ext_prev;
    (* ASYNC_REG = "TRUE" *) reg int_meta;
    (* ASYNC_REG = "TRUE" *) reg int_sync;
    reg int_prev;

    wire ext_rise = ext_sync & ~ext_prev;
    wire int_rise = int_sync & ~int_prev;
    wire ext_quiet = ~ext_sync & ~ext_prev;
    wire int_quiet = ~int_sync & ~int_prev;

    // External qualification.
    reg [CW-1:0] ext_since;     // cycles since the last external rising edge (saturating)
    reg ext_have_prev;          // a previous edge exists to measure a period from
    reg [GW-1:0] ext_good;      // consecutive in-range periods, saturating at GOOD_PERIODS
    wire [CW-1:0] ext_period = ext_since + ONE;
    wire ext_period_ok = ext_have_prev && (ext_period >= P_MIN) && (ext_period <= P_MAX);

    // Selection.
    reg [CW-1:0] pend;          // cycles a requested switch has waited (saturating)
    wire want_ext = ext_alive;
    wire tgt_quiet = want_ext ? ext_quiet : int_quiet;
    wire pend_expired = (pend >= P_MAX);
    wire do_switch = (sel_ext != want_ext) &&
                     ((ext_quiet && int_quiet) || (pend_expired && tgt_quiet));

    // Output regeneration and spacing guard.
    reg [CW-1:0] out_since;     // cycles since the last output edge, saturating at PERIOD_MIN-1
    reg [CW-1:0] pulse_left;
    wire sel_rise = sel_ext ? ext_rise : int_rise;
    wire accept = sel_rise && (out_since >= SPACING_SAT);

    always @(posedge clk) begin
        if (!resetn) begin
            ext_meta <= 1'b1;
            ext_sync <= 1'b1;
            ext_prev <= 1'b1;
            int_meta <= 1'b1;
            int_sync <= 1'b1;
            int_prev <= 1'b1;
            ext_since <= SAT;
            ext_have_prev <= 1'b0;
            ext_good <= {GW{1'b0}};
            ext_alive <= 1'b0;
            sel_ext <= 1'b0;
            pend <= {CW{1'b0}};
            out_since <= SPACING_SAT;
            pulse_left <= {CW{1'b0}};
            pps_out <= 1'b0;
        end else begin
            ext_meta <= ext_pps_async;
            ext_sync <= ext_meta;
            ext_prev <= ext_sync;
            int_meta <= int_pps_async;
            int_sync <= int_meta;
            int_prev <= int_sync;

            if (ext_rise) begin
                if (ext_period_ok) begin
                    if (ext_good != GOOD_N) ext_good <= ext_good + GONE;
                end else begin
                    ext_good <= {GW{1'b0}};
                end
                ext_have_prev <= 1'b1;
                ext_since <= {CW{1'b0}};
            end else begin
                if (ext_since != SAT) ext_since <= ext_since + ONE;
                if (ext_period >= TIMEOUT) begin
                    ext_good <= {GW{1'b0}};
                    ext_have_prev <= 1'b0;
                end
            end
            ext_alive <= (ext_good == GOOD_N);

            if (do_switch) begin
                sel_ext <= want_ext;
                pend <= {CW{1'b0}};
            end else if (sel_ext != want_ext) begin
                if (!pend_expired) pend <= pend + ONE;
            end else begin
                pend <= {CW{1'b0}};
            end

            if (accept) begin
                out_since <= {CW{1'b0}};
                pulse_left <= PULSE_LAST;
                pps_out <= 1'b1;
            end else begin
                if (out_since != SPACING_SAT) out_since <= out_since + ONE;
                if (pulse_left != {CW{1'b0}}) begin
                    pulse_left <= pulse_left - ONE;
                end else begin
                    pps_out <= 1'b0;
                end
            end
        end
    end

endmodule
