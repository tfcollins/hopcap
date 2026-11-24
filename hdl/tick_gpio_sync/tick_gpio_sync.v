// SPDX-License-Identifier: MIT
//
// tick_gpio_sync: Tick's 4-bit FHM GPIO slice into vrt49_fhm_hop_ctrl's clock domain
// (hopcap timed-dma profile, where Tick runs on core_clk_b and the controller stays on
// sys_cpu_clk; libdatax P3b option 1, datax_sdk_sdr#40).
//
// gpio[0] is the hop event and gpio[3:1] the slot index.  Tick writes both in one command,
// on the same tick, and holds them until its next command (microseconds later).  The
// controller samples the slot combinationally on the event's rising edge, so the slot
// must be settled when the event arrives: the slot bits pass two flops, the event three.
// Whichever way each first stage resolves, the slot is valid at least one destination
// cycle before the event can rise.  The first stage of each bit is ASYNC_REG; the XDC
// cuts the path into it.

`timescale 1ns/1ps

module tick_gpio_sync (
    input  wire       clk,
    input  wire [3:0] gpio_async,
    output wire [3:0] gpio_sync
);

    (* ASYNC_REG = "TRUE" *) reg [3:0] gpio_meta = 4'd0;
    (* ASYNC_REG = "TRUE" *) reg [3:0] gpio_s1 = 4'd0;
    reg event_s2 = 1'b0;

    always @(posedge clk) begin
        gpio_meta <= gpio_async;
        gpio_s1 <= gpio_meta;
        event_s2 <= gpio_s1[0];
    end

    assign gpio_sync = {gpio_s1[3:1], event_s2};

endmodule
