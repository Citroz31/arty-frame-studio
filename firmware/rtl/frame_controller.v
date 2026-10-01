`timescale 1ns/1ps
module frame_controller #(
    parameter integer PACKET_TIMEOUT_CYCLES=40000000
) (
    input wire clk, reset,
    input wire [7:0] rx_data,
    input wire rx_valid,
    output wire [7:0] tx_data,
    output wire tx_valid,
    input wire tx_ready,
    output wire busy,
    output wire [15:0] completed,
    output wire data_rise, data_fall, clock_rise, clock_fall,
    output wire latch_rise, latch_fall
);
    wire request_valid;
    wire [7:0] op, seq, length, parser_status;
    wire [255:0] payload;
    packet_rx #(.TIMEOUT_CYCLES(PACKET_TIMEOUT_CYCLES)) parser (
        .clk(clk), .reset(reset), .rx_data(rx_data), .rx_valid(rx_valid),
        .request_valid(request_valid), .request_op(op), .request_seq(seq),
        .request_length(length), .request_status(parser_status),
        .request_payload(payload)
    );

    // Bounded reply queue absorbs modest back-to-back traffic. The host must
    // keep one request in flight. A saturated queue discards a request without
    // performing its command, so a timeout cannot hide an unacknowledged SEND.
    reg [47:0] replies [0:3];
    reg [1:0] read_pointer, write_pointer;
    reg [2:0] reply_count;
    reg sending;
    reg preparing;
    reg [3:0] tx_index;
    reg [3:0] crc_index;
    reg [15:0] response_crc;
    reg [95:0] tx_packet;
    wire take_reply = !sending && !preparing && reply_count != 0;
    wire accept_request = request_valid && (reply_count < 4 || take_reply);
    reg [7:0] status, reply_busy;
    reg [15:0] reply_completed;

    wire send_payload_valid = length == 14
        && payload[39:32] >= 1 && payload[39:32] <= 26
        && (payload[31:0] >> payload[39:32]) == 0
        && payload[55:40] != 0 && payload[71:56] != 0
        && payload[103:88] != 0 && payload[111:104] <= 3;
    wire start = accept_request && status == 0 && op == 2;
    wire stop = accept_request && status == 0 && op == 3;

    always @* begin
        status = parser_status;
        reply_busy = {7'b0, busy};
        reply_completed = completed;
        if (parser_status == 0) begin
            case (op)
                1, 3, 4: if (length != 0) status = 2;
                2: if (!send_payload_valid) status = 2;
                   else if (busy) status = 3;
                default: status = 1;
            endcase
        end
        if (status == 0 && op == 2) begin
            reply_busy = 1;
            reply_completed = 0;
        end else if (status == 0 && op == 3) begin
            reply_busy = 0;
        end
    end

    frame_engine engine (
        .clk(clk), .reset(reset), .start(start), .stop(stop),
        .word_in(payload[31:0]), .bits_in(payload[36:32]),
        .divider_in(payload[55:40]), .latch_ticks_in(payload[71:56]),
        .gap_ticks_in(payload[87:72]), .repeat_in(payload[103:88]),
        .flags_in(payload[105:104]), .busy(busy), .completed(completed),
        .data_rise(data_rise), .data_fall(data_fall),
        .clock_rise(clock_rise), .clock_fall(clock_fall),
        .latch_rise(latch_rise), .latch_fall(latch_fall)
    );

    function [15:0] crc_byte;
        input [15:0] previous;
        input [7:0] value;
        integer bit_index;
        reg [15:0] current;
        begin
            current = previous ^ {value, 8'b0};
            for (bit_index=0; bit_index<8; bit_index=bit_index+1)
                current = current[15] ? (current << 1) ^ 16'h1021
                                      : current << 1;
            crc_byte = current;
        end
    endfunction

    function [95:0] response_packet;
        input [47:0] reply;
        reg [95:0] packet;
        begin
            // reply={completed, busy, status, seq, op|80}, little endian.
            packet = 0;
            packet[7:0] = 8'ha7;
            packet[15:8] = 8'h7a;
            packet[23:16] = 1;
            packet[31:24] = reply[7:0];
            packet[39:32] = reply[15:8];
            packet[47:40] = 4;
            packet[79:48] = reply[47:16];
            response_packet = packet;
        end
    endfunction

    assign tx_data = tx_packet[tx_index*8 +: 8];
    assign tx_valid = sending;
    always @(posedge clk) begin
        if (reset) begin
            read_pointer <= 0;
            write_pointer <= 0;
            reply_count <= 0;
            sending <= 0;
            preparing <= 0;
            tx_index <= 0;
            crc_index <= 2;
            response_crc <= 16'hffff;
            tx_packet <= 0;
        end else begin
            case ({accept_request, take_reply})
                2'b10: reply_count <= reply_count+1'b1;
                2'b01: reply_count <= reply_count-1'b1;
                default: reply_count <= reply_count;
            endcase
            if (accept_request) begin
                replies[write_pointer] <= {
                    reply_completed, reply_busy, status, seq, op | 8'h80
                };
                write_pointer <= write_pointer+1'b1;
            end
            if (take_reply) begin
                tx_packet <= response_packet(replies[read_pointer]);
                read_pointer <= read_pointer+1'b1;
                tx_index <= 0;
                crc_index <= 2;
                response_crc <= 16'hffff;
                preparing <= 1;
            end else if (preparing) begin
                // One CRC byte per cycle, avoiding a 64-bit combinatorial
                // cascade on the 200 MHz response preparation path.
                response_crc <= crc_byte(response_crc, tx_packet[crc_index*8 +: 8]);
                if (crc_index == 9) begin
                    tx_packet[95:80] <= crc_byte(response_crc, tx_packet[crc_index*8 +: 8]);
                    preparing <= 0;
                    sending <= 1;
                end else crc_index <= crc_index+1'b1;
            end else if (tx_valid && tx_ready) begin
                if (tx_index == 11) sending <= 0;
                else tx_index <= tx_index+1'b1;
            end
        end
    end
endmodule
