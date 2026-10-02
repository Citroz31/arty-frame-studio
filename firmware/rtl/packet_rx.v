`timescale 1ns/1ps
// Packet bytes: A7 7A version op seq length payload CRC-lo CRC-hi.
// Oversize packets are consumed in full, then rejected; partial packets expire.
module packet_rx #(
    parameter integer TIMEOUT_CYCLES=40000000
) (
    input wire clk, reset,
    input wire [7:0] rx_data,
    input wire rx_valid,
    output reg request_valid,
    output reg [7:0] request_op, request_seq, request_length, request_status,
    output reg [255:0] request_payload
);
    localparam SYNC_A7=0, SYNC_7A=1, VERSION=2, OP=3, SEQ=4,
               LENGTH=5, PAYLOAD=6, CRC_LOW=7, CRC_HIGH=8;
    localparam integer TIMEOUT_WIDTH=TIMEOUT_CYCLES <= 2 ? 1 : $clog2(TIMEOUT_CYCLES);
    reg [3:0] state;
    reg [7:0] version, op, seq, length, payload_index, crc_low;
    reg [7:0] last_payload_index;
    reg [255:0] payload;
    reg [15:0] crc;
    reg [TIMEOUT_WIDTH-1:0] timeout_count;
    integer payload_byte;

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

    always @(posedge clk) begin
        if (reset) begin
            state <= SYNC_A7;
            version <= 0;
            op <= 0;
            seq <= 0;
            length <= 0;
            payload_index <= 0;
            last_payload_index <= 0;
            crc_low <= 0;
            crc <= 16'hffff;
            payload <= 0;
            timeout_count <= 0;
            request_valid <= 0;
            request_op <= 0;
            request_seq <= 0;
            request_length <= 0;
            request_status <= 0;
            request_payload <= 0;
        end else begin
            request_valid <= 0;
            if (rx_valid) begin
                timeout_count <= 0;
                case (state)
                    SYNC_A7: if (rx_data == 8'ha7) state <= SYNC_7A;
                    SYNC_7A: if (rx_data == 8'h7a) state <= VERSION;
                             else if (rx_data != 8'ha7) state <= SYNC_A7;
                    VERSION: begin
                        version <= rx_data;
                        crc <= crc_byte(16'hffff, rx_data);
                        state <= OP;
                    end
                    OP: begin
                        op <= rx_data;
                        crc <= crc_byte(crc, rx_data);
                        state <= SEQ;
                    end
                    SEQ: begin
                        seq <= rx_data;
                        crc <= crc_byte(crc, rx_data);
                        state <= LENGTH;
                    end
                    LENGTH: begin
                        length <= rx_data;
                        last_payload_index <= rx_data-1'b1;
                        payload <= 0;
                        payload_index <= 0;
                        crc <= crc_byte(crc, rx_data);
                        state <= rx_data == 0 ? CRC_LOW : PAYLOAD;
                    end
                    PAYLOAD: begin
                        // Constant byte slices become independent write enables.
                        // A variable part-select otherwise creates a wide
                        // arithmetic/shifter path from the received byte index.
                        for (payload_byte=0; payload_byte<32; payload_byte=payload_byte+1)
                            if (payload_index == payload_byte)
                                payload[payload_byte*8 +: 8] <= rx_data;
                        crc <= crc_byte(crc, rx_data);
                        payload_index <= payload_index+1'b1;
                        if (payload_index == last_payload_index) state <= CRC_LOW;
                    end
                    CRC_LOW: begin
                        crc_low <= rx_data;
                        state <= CRC_HIGH;
                    end
                    CRC_HIGH: begin
                        request_op <= op;
                        request_seq <= seq;
                        request_length <= length;
                        request_payload <= payload;
                        request_status <= {rx_data, crc_low} != crc ? 4
                                          : version != 1 ? 5
                                          : length > 32 ? 2 : 0;
                        request_valid <= 1;
                        state <= SYNC_A7;
                    end
                    default: state <= SYNC_A7;
                endcase
            end else if (state != SYNC_A7) begin
                if (timeout_count >= TIMEOUT_CYCLES-1) begin
                    state <= SYNC_A7;
                    timeout_count <= 0;
                end else timeout_count <= timeout_count+1'b1;
            end
        end
    end
endmodule
