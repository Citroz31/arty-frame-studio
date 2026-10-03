`timescale 1ns/1ps
module frame_controller #(
    parameter integer PACKET_TIMEOUT_CYCLES=40000000,
    // INFO pages 0-5: revision, CORE_HZ low/high, capabilities, BUILD_ID low/high.
    parameter [15:0] FIRMWARE_REVISION=16'd2,
    parameter [31:0] CORE_HZ=32'd200000000,
    parameter [15:0] CAPABILITIES=16'h0003,
    parameter [31:0] BUILD_ID=32'h0
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
    output wire latch_rise, latch_fall,
    // One-cycle LED command, at reply acceptance: {manual, pattern[3:0]}.
    output wire led_write,
    output wire [4:0] led_value
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

    // The parser publishes its payload and request_valid together. Capture
    // both on the following clock, then validate the captured request before
    // driving the engine. No payload-dependent validation lies on START's
    // high-fanout path to the 200 MHz engine registers.
    reg captured_valid, captured_empty, captured_send_shape;
    reg captured_led_shape, captured_info_shape;
    reg [7:0] captured_op, captured_seq, captured_parser_status;
    reg [111:0] captured_payload;
    reg [4:0] captured_shift_amount;
    reg [31:0] captured_word_overflow;
    reg validated_valid, send_eligible, stop_eligible, led_eligible;
    reg [15:0] validated_info;
    reg [7:0] validated_op, validated_seq, validated_status;
    reg [111:0] validated_payload;
    reg [25:0] validated_aligned_word;
    reg [7:0] next_static_status;
    wire [31:0] word_overflow;
    genvar word_bit;
    generate
        for (word_bit=0; word_bit<32; word_bit=word_bit+1) begin: validate_word
            // Compare each occupied bit against the requested width instead
            // of synthesizing an eight-bit-controlled 32-bit barrel shifter.
            assign word_overflow[word_bit] = payload[word_bit]
                && payload[39:32] <= word_bit;
        end
    endgenerate

    always @* begin
        next_static_status = captured_parser_status;
        if (captured_parser_status == 0) begin
            case (captured_op)
                1, 3, 4: if (!captured_empty) next_static_status = 2;
                2: if (!captured_send_shape || |captured_word_overflow)
                       next_static_status = 2;
                5: if (!captured_led_shape) next_static_status = 2;
                6: if (!captured_info_shape) next_static_status = 2;
                default: next_static_status = 1;
            endcase
        end
    end

    function [15:0] info_word;
        input [2:0] page;
        begin
            case (page)
                0: info_word = FIRMWARE_REVISION;
                1: info_word = CORE_HZ[15:0];
                2: info_word = CORE_HZ[31:16];
                3: info_word = CAPABILITIES;
                4: info_word = BUILD_ID[15:0];
                default: info_word = BUILD_ID[31:16];
            endcase
        end
    endfunction

    always @(posedge clk) begin
        if (reset) begin
            captured_valid <= 0;
            captured_empty <= 0;
            captured_send_shape <= 0;
            captured_led_shape <= 0;
            captured_info_shape <= 0;
            captured_op <= 0;
            captured_seq <= 0;
            captured_parser_status <= 0;
            captured_payload <= 0;
            captured_shift_amount <= 0;
            captured_word_overflow <= 0;
            validated_valid <= 0;
            send_eligible <= 0;
            stop_eligible <= 0;
            led_eligible <= 0;
            validated_info <= 0;
            validated_op <= 0;
            validated_seq <= 0;
            validated_status <= 0;
            validated_payload <= 0;
            validated_aligned_word <= 0;
        end else begin
            // Unconditional data registers avoid making request_valid a
            // clock-enable for every configuration and validation bit.
            captured_valid <= request_valid;
            captured_empty <= length == 0;
            captured_send_shape <= length == 14
                && payload[39:32] >= 1 && payload[39:32] <= 26
                && payload[55:40] != 0 && payload[71:56] != 0
                && payload[103:88] != 0 && payload[111:104] <= 3;
            // LED: {manual, 3'b0, pattern[3:0]}. INFO: one page byte, 0 to 5.
            captured_led_shape <= length == 1 && payload[6:4] == 0;
            captured_info_shape <= length == 1 && payload[7:0] <= 5;
            captured_op <= op;
            captured_seq <= seq;
            captured_parser_status <= parser_status;
            captured_payload <= payload[111:0];
            captured_shift_amount <= 5'd26 - payload[36:32];
            captured_word_overflow <= word_overflow;
            validated_valid <= captured_valid;
            // Register complete command eligibility separately from the
            // eight-bit response status. START/STOP only need these bits,
            // current queue capacity, and the engine's current BUSY state.
            send_eligible <= captured_valid && captured_parser_status == 0
                && captured_op == 2 && captured_send_shape
                && !(|captured_word_overflow);
            stop_eligible <= captured_valid && captured_parser_status == 0
                && captured_op == 3 && captured_empty;
            led_eligible <= captured_valid && captured_parser_status == 0
                && captured_op == 5 && captured_led_shape;
            validated_info <= info_word(captured_payload[2:0]);
            validated_op <= captured_op;
            validated_seq <= captured_seq;
            validated_status <= next_static_status;
            validated_payload <= captured_payload;
            // Prepare the MSB-first shift register before START reaches the
            // engine. LSB-first uses the original low bits without alignment.
            validated_aligned_word <= captured_payload[104]
                ? captured_payload[25:0]
                : captured_payload[25:0] << captured_shift_amount;
        end
    end

    // Bounded reply queue absorbs modest back-to-back traffic. The host must
    // keep one request in flight. A saturated queue discards a request without
    // performing its command, so a timeout cannot hide an unacknowledged SEND.
    reg [47:0] replies [0:3];
    reg [1:0] read_pointer, write_pointer;
    reg [2:0] reply_count;
    reg write_pending;
    reg [1:0] pending_address;
    reg [47:0] pending_reply;
    reg sending;
    reg preparing;
    reg [3:0] tx_index;
    reg [3:0] crc_index;
    reg [15:0] response_crc;
    reg [95:0] tx_packet;
    wire take_reply = !sending && !preparing && reply_count != 0;
    // reply_count covers committed entries only. An accepted reply reserves
    // one slot until its registered write is committed on the next edge.
    // reply_room is that free-slot test, registered one cycle ahead from the
    // exact next occupancy: START/STOP/LED then depend on three flip-flops,
    // not on the reply transmitter state, before their wide enable fanout.
    // A pop in the same cycle is not counted as room: conservative only.
    reg reply_room;
    wire reply_capacity = reply_room;
    wire accept_request = validated_valid && reply_capacity;
    reg [7:0] status, reply_busy;
    reg [15:0] reply_completed;

    wire start = send_eligible && reply_capacity && !busy;
    wire stop = stop_eligible && reply_capacity;
    assign led_write = led_eligible && reply_capacity;
    assign led_value = {validated_payload[7], validated_payload[3:0]};

    always @* begin
        status = validated_status;
        reply_busy = {7'b0, busy};
        reply_completed = completed;
        // BUSY and completion are sampled at actual acceptance, rather than
        // when the request entered the validation pipeline.
        if (send_eligible && busy) status = 3;
        // INFO returns its page word in the 16-bit field of the common reply.
        if (validated_op == 6 && validated_status == 0) reply_completed = validated_info;
        if (start) begin
            reply_busy = 1;
            reply_completed = 0;
        end else if (stop) begin
            reply_busy = 0;
        end
    end

    frame_engine #(.WORD_PREALIGNED(1)) engine (
        .clk(clk), .reset(reset), .start(start), .stop(stop),
        .word_in({6'b0, validated_aligned_word}), .bits_in(validated_payload[36:32]),
        .divider_in(validated_payload[55:40]), .latch_ticks_in(validated_payload[71:56]),
        .gap_ticks_in(validated_payload[87:72]), .repeat_in(validated_payload[103:88]),
        .flags_in(validated_payload[105:104]), .busy(busy), .completed(completed),
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
            reply_room <= 1;
            write_pending <= 0;
            pending_address <= 0;
            pending_reply <= 0;
            sending <= 0;
            preparing <= 0;
            tx_index <= 0;
            crc_index <= 2;
            response_crc <= 16'hffff;
            tx_packet <= 0;
        end else begin
            case ({write_pending, take_reply})
                2'b10: reply_count <= reply_count+1'b1;
                2'b01: reply_count <= reply_count-1'b1;
                default: reply_count <= reply_count;
            endcase
            // Next occupancy = committed + pending write - pop + new request.
            reply_room <= {1'b0, reply_count} + write_pending + accept_request
                - take_reply < 4;
            // Snapshot the reply at the same edge that executes START/STOP.
            // Only the write enable is conditional; unconditional data and
            // address registers keep queue-capacity logic off their enables.
            write_pending <= accept_request;
            pending_reply <= {
                reply_completed, reply_busy, status, validated_seq, validated_op | 8'h80
            };
            pending_address <= write_pointer;
            if (write_pending) replies[pending_address] <= pending_reply;
            if (accept_request) begin
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
