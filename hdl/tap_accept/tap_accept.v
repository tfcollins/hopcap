// SPDX-License-Identifier: MIT
//
// tap_accept: one pulse per sample the vrt49 tap actually forwarded (hopcap adrv9361).
//
// vrt49_sample_tap never stalls its source: when its one-entry register cannot be replaced
// it drops the newly selected beat and increments dropped_sample_count on the next clock.
// The framer stamps a packet from the beats it has consumed since the PPS; the time base
// counts beats since the PPS. If the time base counts the tap's *input* (sample_valid) while
// the framer only ever sees *accepted* beats, every drop puts the framer permanently behind
// the time base -- on lablp ~1.3 s of drops during bring-up shifted every later stamp, or
// (after the framer's >65536-beat resync) left an arbitrary sub-second offset.
//
// accepted is sample_valid delayed one clock with the beats that were dropped removed, so
// the time base and the framer count exactly the same beats. The fixed one-clock delay moves
// every beat relative to the PPS by the same amount (well under one sample at the AD9361
// divclk rate) and is part of the per-image latency constant. A dropped beat now simply does
// not exist for either side: the second it falls in is short by that many beats, which the
// time base reports as that second's period.

`timescale 1ns/1ps

module tap_accept (
    input  wire        clk,
    input  wire        resetn,
    input  wire        sample_valid,          // vrt49_sample_tap/sample_valid (DECIMATION 1)
    input  wire [31:0] dropped_sample_count,  // vrt49_sample_tap/dropped_sample_count
    output reg         accepted = 1'b0
);

    reg        valid_d = 1'b0;
    reg [31:0] dropped_d = 32'd0;

    always @(posedge clk) begin
        if (!resetn) begin
            valid_d   <= 1'b0;
            dropped_d <= 32'd0;
            accepted  <= 1'b0;
        end else begin
            valid_d   <= sample_valid;
            dropped_d <= dropped_sample_count;
            // The tap counts a drop on the same edge that samples the beat: compare the
            // count after that edge with the count before it.
            accepted  <= valid_d && (dropped_sample_count == dropped_d);
        end
    end

endmodule
