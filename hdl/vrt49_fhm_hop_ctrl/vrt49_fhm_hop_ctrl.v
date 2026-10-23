// SPDX-License-Identifier: BSD-2-Clause
// Derived from vrt49/hdl/vrt49_ad9361_hop_ctrl/vrt49_ad9361_hop_ctrl.v (BSD-2-Clause).
//
// Talise FHM hop-gated capture controller.
//
// Tick drives the Talise FHM trigger pin directly; this block watches the same
// scheduler GPIO.  A rising edge on scheduler_gpio[0] is a hop event.  The slot
// index (one of eight host-written Q44.20 frequency words) comes from
// scheduler_gpio[3:1], or from an auto-incrementing sequence index so a Tick
// REPEAT pair can drive a periodic plan.
//
// On an event the sample gate closes, the framer pipe is drained, SETTLE_TICKS
// elapse, one changed Context (slot frequency + hop epoch) is handed to the
// framer, and only once the framer reports the Context packet left does the
// gate open for CAPTURE_TICKS (0 = until the next event).
//
// Unlike the AD9361 bridge there is no pending FIFO: on Talise the PLL has
// already hopped when the edge arrives, so an event that lands before the
// previous hop's Context is accepted simply wins ("latest wins") and is
// counted in SUPERSEDED_COUNT.  EPOCH still increments once per event, so a
// consumer can see from the epoch gap that a Context was never published.
//
// enable=0 forces the gate open and holds IDLE, reproducing the untied
// (gapless) vrt49 behaviour.
//
// Register map: hdl/hopctrl_regmap.json is authoritative.
`timescale 1ns/1ps
module vrt49_fhm_hop_ctrl #(
    parameter integer AXI_ADDR_WIDTH = 8,
    parameter integer DEFAULT_SETTLE_TICKS = 2000,
    parameter integer DEFAULT_CAPTURE_TICKS = 0,
    parameter integer DEFAULT_IDLE_OPEN = 1,
    parameter integer DEFAULT_DRAIN_TIMEOUT = 0
)(
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF S_AXI, ASSOCIATED_RESET resetn" *)
    input wire clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 resetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input wire resetn,
    input wire [3:0] scheduler_gpio,
    output wire stream_enable,
    output wire gate_open,
    output wire [2:0] state_out,
    input wire fabric_samples_drained,
    output reg fabric_context_valid,
    input wire fabric_context_ready,
    output reg [63:0] fabric_rf_frequency_q44_20,
    output reg [31:0] fabric_hop_epoch,
    input wire fabric_context_accepted,

    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWADDR" *)
    input wire [AXI_ADDR_WIDTH-1:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWPROT" *)
    input wire [2:0] s_axi_awprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *)
    input wire s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *)
    output reg s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WDATA" *)
    input wire [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *)
    input wire [3:0] s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WVALID" *)
    input wire s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WREADY" *)
    output reg s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BRESP" *)
    output reg [1:0] s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BVALID" *)
    output reg s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BREADY" *)
    input wire s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *)
    input wire [AXI_ADDR_WIDTH-1:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARPROT" *)
    input wire [2:0] s_axi_arprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *)
    input wire s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *)
    output reg s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RDATA" *)
    output reg [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RRESP" *)
    output reg [1:0] s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RVALID" *)
    output reg s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RREADY" *)
    input wire s_axi_rready
);
    localparam [31:0] ID_WORD      = 32'h484F5032; // "HOP2"
    localparam [31:0] VERSION_WORD = 32'h00010000;

    localparam [2:0] IDLE=3'd0, DRAIN=3'd1, SETTLE=3'd2, PUBLISH=3'd3, OPEN=3'd4, CLOSED=3'd5;

    // --- control/config -------------------------------------------------
    reg enabled, slot_source_seq, idle_open;
    reg [31:0] settle_ticks, capture_ticks, drain_timeout_ticks;
    reg [31:0] seq_slot_count, seq_index;
    reg [63:0] slot_frequency [0:7];

    // --- state -----------------------------------------------------------
    reg [2:0] state;
    reg [3:0] gpio_seen;
    reg [2:0] last_slot;
    reg [31:0] settle_left, capture_left, drain_elapsed;
    reg published;          // PUBLISH: framer has latched the tuple (valid&ready seen)
    reg pending;            // PUBLISH: an event landed after the handshake
    reg superseded_sticky, drain_timeout_sticky;
    reg [31:0] superseded_count, event_count, capture_count;

    wire event_edge = enabled && scheduler_gpio[0] && !gpio_seen[0];
    wire [2:0] slot_sel = slot_source_seq ? seq_index[2:0] : scheduler_gpio[3:1];
    wire [31:0] seq_next = (seq_index + 32'd1 >= seq_slot_count) ? 32'd0 : seq_index + 32'd1;

    assign stream_enable = !enabled ? 1'b1 :
                           (state == IDLE) ? idle_open :
                           (state == OPEN);
    assign gate_open = stream_enable;
    assign state_out = state;

    // --- AXI-Lite write/read machinery (as in the AD9361 bridge) -------------
    reg [7:0] awaddr_hold;
    reg [31:0] wdata_hold;
    reg [3:0] wstrb_hold;
    reg aw_hold_valid, w_hold_valid;
    reg [31:0] read_data;
    integer i;

    function [31:0] apply_wstrb;
        input [31:0] old_value, new_value;
        input [3:0] strobe;
        begin
            apply_wstrb = old_value;
            if (strobe[0]) apply_wstrb[7:0]   = new_value[7:0];
            if (strobe[1]) apply_wstrb[15:8]  = new_value[15:8];
            if (strobe[2]) apply_wstrb[23:16] = new_value[23:16];
            if (strobe[3]) apply_wstrb[31:24] = new_value[31:24];
        end
    endfunction

    always @(*) begin
        read_data = 32'd0;
        case (s_axi_araddr)
            8'h00: read_data = ID_WORD;
            8'h04: read_data = VERSION_WORD;
            8'h08: begin
                read_data[2:0] = state;
                read_data[3]   = stream_enable;
                read_data[4]   = superseded_sticky;
                read_data[5]   = drain_timeout_sticky;
                read_data[8]   = enabled;
                read_data[9]   = slot_source_seq;
                read_data[11]  = idle_open;
            end
            8'h0c: read_data = settle_ticks;
            8'h10: read_data = {28'd0, gpio_seen};
            8'h14: read_data = fabric_hop_epoch;
            8'h18: read_data = capture_ticks;
            8'h1c: read_data = superseded_count;
            8'h20: read_data = event_count;
            8'h24: read_data = capture_count;
            8'h28: read_data = {29'd0, last_slot};
            8'h2c: read_data = seq_slot_count;
            8'h30: read_data = seq_index;
            8'h34: read_data = drain_timeout_ticks;
            default: begin
                if (s_axi_araddr[7:6] == 2'b01) begin
                    // 0x40..0x7C: slot table, lo word at +0, hi word at +4
                    read_data = s_axi_araddr[2] ? slot_frequency[s_axi_araddr[5:3]][63:32]
                                                : slot_frequency[s_axi_araddr[5:3]][31:0];
                end
            end
        endcase
    end

    // Shared "take an event" action.
    task automatic take_event;
        begin
            last_slot <= slot_sel;
            fabric_rf_frequency_q44_20 <= slot_frequency[slot_sel];
            fabric_hop_epoch <= fabric_hop_epoch + 32'd1;
            event_count <= event_count + 32'd1;
            if (slot_source_seq) seq_index <= seq_next;
        end
    endtask

    always @(posedge clk) begin
        if (!resetn) begin
            state <= IDLE;
            enabled <= 1'b0;
            slot_source_seq <= 1'b0;
            idle_open <= (DEFAULT_IDLE_OPEN != 0);
            settle_ticks <= DEFAULT_SETTLE_TICKS;
            capture_ticks <= DEFAULT_CAPTURE_TICKS;
            drain_timeout_ticks <= DEFAULT_DRAIN_TIMEOUT;
            seq_slot_count <= 32'd8;
            seq_index <= 32'd0;
            gpio_seen <= 4'd0;
            last_slot <= 3'd0;
            settle_left <= 32'd0;
            capture_left <= 32'd0;
            drain_elapsed <= 32'd0;
            published <= 1'b0;
            pending <= 1'b0;
            superseded_sticky <= 1'b0;
            drain_timeout_sticky <= 1'b0;
            superseded_count <= 32'd0;
            event_count <= 32'd0;
            capture_count <= 32'd0;
            fabric_context_valid <= 1'b0;
            fabric_rf_frequency_q44_20 <= 64'd0;
            fabric_hop_epoch <= 32'd0;
            s_axi_awready <= 0; s_axi_wready <= 0;
            s_axi_bresp <= 0; s_axi_bvalid <= 0;
            s_axi_arready <= 0; s_axi_rresp <= 0; s_axi_rvalid <= 0;
            s_axi_rdata <= 0;
            aw_hold_valid <= 0; w_hold_valid <= 0;
            awaddr_hold <= 0; wdata_hold <= 0; wstrb_hold <= 0;
            for (i = 0; i < 8; i = i + 1) slot_frequency[i] <= 64'd0;
        end else begin
            // ---------------- AXI-Lite write ----------------
            s_axi_awready <= 0;
            s_axi_wready <= 0;
            s_axi_arready <= 0;
            if (s_axi_awvalid && !aw_hold_valid && !s_axi_bvalid) begin
                s_axi_awready <= 1;
                awaddr_hold <= s_axi_awaddr;
                aw_hold_valid <= 1;
            end
            if (s_axi_wvalid && !w_hold_valid && !s_axi_bvalid) begin
                s_axi_wready <= 1;
                wdata_hold <= s_axi_wdata;
                wstrb_hold <= s_axi_wstrb;
                w_hold_valid <= 1;
            end
            if (aw_hold_valid && w_hold_valid && !s_axi_bvalid) begin
                s_axi_bvalid <= 1;
                s_axi_bresp <= 0;
                aw_hold_valid <= 0;
                w_hold_valid <= 0;
                case (awaddr_hold)
                    8'h08: if (wstrb_hold[0]) begin
                        enabled <= wdata_hold[0];
                        slot_source_seq <= wdata_hold[1];
                        idle_open <= wdata_hold[3];
                        if (wdata_hold[2]) begin
                            superseded_sticky <= 1'b0;
                            drain_timeout_sticky <= 1'b0;
                            superseded_count <= 32'd0;
                        end
                    end
                    8'h0c: settle_ticks <= apply_wstrb(settle_ticks, wdata_hold, wstrb_hold);
                    8'h18: capture_ticks <= apply_wstrb(capture_ticks, wdata_hold, wstrb_hold);
                    8'h2c: seq_slot_count <= apply_wstrb(seq_slot_count, wdata_hold, wstrb_hold);
                    8'h30: seq_index <= apply_wstrb(seq_index, wdata_hold, wstrb_hold);
                    8'h34: drain_timeout_ticks <= apply_wstrb(drain_timeout_ticks, wdata_hold, wstrb_hold);
                    default: begin
                        if (awaddr_hold[7:6] == 2'b01) begin
                            if (awaddr_hold[2])
                                slot_frequency[awaddr_hold[5:3]][63:32] <=
                                    apply_wstrb(slot_frequency[awaddr_hold[5:3]][63:32], wdata_hold, wstrb_hold);
                            else
                                slot_frequency[awaddr_hold[5:3]][31:0] <=
                                    apply_wstrb(slot_frequency[awaddr_hold[5:3]][31:0], wdata_hold, wstrb_hold);
                        end
                    end
                endcase
            end else if (s_axi_bvalid && s_axi_bready) s_axi_bvalid <= 0;

            // ---------------- AXI-Lite read ----------------
            if (s_axi_arvalid && !s_axi_rvalid) begin
                s_axi_arready <= 1;
                s_axi_rvalid <= 1;
                s_axi_rdata <= read_data;
                s_axi_rresp <= 0;
            end else if (s_axi_rvalid && s_axi_rready) s_axi_rvalid <= 0;

            // ---------------- hop gate state machine ----------------
            gpio_seen <= scheduler_gpio;

            if (!enabled) begin
                state <= IDLE;
                fabric_context_valid <= 1'b0;
                published <= 1'b0;
                pending <= 1'b0;
            end else begin
                case (state)
                    IDLE: begin
                        if (event_edge) begin
                            take_event;
                            drain_elapsed <= 32'd0;
                            state <= DRAIN;
                        end
                    end
                    DRAIN: begin
                        if (event_edge) begin
                            take_event;
                            superseded_sticky <= 1'b1;
                            if (superseded_count != 32'hffffffff)
                                superseded_count <= superseded_count + 32'd1;
                        end
                        drain_elapsed <= drain_elapsed + 32'd1;
                        if (fabric_samples_drained) begin
                            settle_left <= settle_ticks;
                            state <= SETTLE;
                        end else if (drain_timeout_ticks != 32'd0 && drain_elapsed >= drain_timeout_ticks) begin
                            drain_timeout_sticky <= 1'b1;
                            settle_left <= settle_ticks;
                            state <= SETTLE;
                        end
                    end
                    SETTLE: begin
                        if (event_edge) begin
                            // The PLL moved again: restart the drain/settle for the new hop.
                            take_event;
                            superseded_sticky <= 1'b1;
                            if (superseded_count != 32'hffffffff)
                                superseded_count <= superseded_count + 32'd1;
                            drain_elapsed <= 32'd0;
                            state <= DRAIN;
                        end else if (settle_left == 32'd0) begin
                            fabric_context_valid <= 1'b1;
                            published <= 1'b0;
                            pending <= 1'b0;
                            state <= PUBLISH;
                        end else begin
                            settle_left <= settle_left - 32'd1;
                        end
                    end
                    PUBLISH: begin
                        if (fabric_context_valid && fabric_context_ready) begin
                            fabric_context_valid <= 1'b0;
                            published <= 1'b1;
                        end
                        if (event_edge) begin
                            // Before the handshake the framer has not latched anything,
                            // so the refreshed tuple simply replaces it.  After the
                            // handshake the in-flight Context is stale: remember to
                            // run the cycle again once it has left.
                            take_event;
                            superseded_sticky <= 1'b1;
                            if (superseded_count != 32'hffffffff)
                                superseded_count <= superseded_count + 32'd1;
                            // "Published" includes the handshake happening this very cycle:
                            // the framer has latched the OLD tuple, so a rerun is needed.
                            if (published || (fabric_context_valid && fabric_context_ready))
                                pending <= 1'b1;
                        end
                        if (fabric_context_accepted) begin
                            fabric_context_valid <= 1'b0;
                            published <= 1'b0;
                            if (pending || (event_edge && (published || (fabric_context_valid && fabric_context_ready)))) begin
                                pending <= 1'b0;
                                drain_elapsed <= 32'd0;
                                state <= DRAIN;
                            end else begin
                                capture_left <= capture_ticks;
                                state <= OPEN;
                            end
                        end
                    end
                    OPEN: begin
                        if (event_edge) begin
                            take_event;
                            drain_elapsed <= 32'd0;
                            state <= DRAIN;
                        end else if (capture_ticks != 32'd0) begin
                            if (capture_left <= 32'd1) begin
                                capture_count <= capture_count + 32'd1;
                                state <= CLOSED;
                            end else begin
                                capture_left <= capture_left - 32'd1;
                            end
                        end
                    end
                    CLOSED: begin
                        if (event_edge) begin
                            take_event;
                            drain_elapsed <= 32'd0;
                            state <= DRAIN;
                        end
                    end
                    default: state <= IDLE;
                endcase
            end
        end
    end
endmodule
