// SPDX-License-Identifier: MIT
//
// tick_pps_gen: an internal 1 Hz pulse on the Tick clock (hopcap adrv9361 profile).
//
// The ADRV9361-Z7035 composite has no GPS or board PPS.  VRT time is defined as Tick time
// (spec decision 3): this generator drives both Tick's pps_in and vrt49_time_base's pps_in,
// so a Tick TIME_EPOCH load and a framer seconds load armed for the same edge describe the
// same second.  The second is TICK_HZ tick-clock cycles exactly, so Tick time advances by
// exactly TICK_HZ between consecutive rising edges.
//
// Timing: with resetn released before rising clock edge 0 (counter 0), pps_out is high from
// edge 0 for PULSE_TICKS cycles, then low until edge TICK_HZ, where the next pulse starts.
// Rising edges of pps_out are exactly TICK_HZ cycles apart; the first is one register delay
// after reset release.  Held low during reset.
//
// Parameters must satisfy 0 < PULSE_TICKS < TICK_HZ; elaboration fails otherwise.

`timescale 1ns/1ps

module tick_pps_gen #(
    parameter integer TICK_HZ = 50000000,
    parameter integer PULSE_TICKS = 5000000
)(
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_RESET resetn" *)
    input wire clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 resetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input wire resetn,
    output reg pps_out
);

    localparam integer CW = $clog2(TICK_HZ);
    localparam integer LAST_I = TICK_HZ - 1;
    localparam [CW-1:0] LAST = LAST_I[CW-1:0];
    localparam [CW-1:0] PULSE = PULSE_TICKS[CW-1:0];
    localparam [CW-1:0] ONE = {{(CW-1){1'b0}}, 1'b1};

    // Elaboration-time parameter check (Verilog-2005: an unresolvable module fails the build).
    generate
        if (!(PULSE_TICKS > 0 && PULSE_TICKS < TICK_HZ && TICK_HZ > 1)) begin : g_bad_parameters
            tick_pps_gen_requires_0_lt_PULSE_TICKS_lt_TICK_HZ u_bad_parameters ();
        end
    endgenerate

    reg [CW-1:0] count = {CW{1'b0}};

    initial pps_out = 1'b0;

    always @(posedge clk) begin
        if (!resetn) begin
            count <= {CW{1'b0}};
            pps_out <= 1'b0;
        end else begin
            count <= (count == LAST) ? {CW{1'b0}} : count + ONE;
            pps_out <= (count < PULSE);
        end
    end

endmodule
