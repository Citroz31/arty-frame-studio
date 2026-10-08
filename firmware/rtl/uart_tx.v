`timescale 1ns/1ps
module uart_tx #(
    parameter integer CLOCK_HZ=100000000,
    parameter integer BAUD=115200
) (
    input wire clk, reset,
    input wire [7:0] data,
    input wire valid,
    output wire ready,
    output wire tx
);
    localparam integer CLKS_PER_BIT=(CLOCK_HZ + BAUD/2)/BAUD;
    localparam integer COUNT_WIDTH=CLKS_PER_BIT <= 2 ? 1 : $clog2(CLKS_PER_BIT);
    reg [9:0] shift;
    reg [3:0] bits_left;
    reg [COUNT_WIDTH-1:0] count;
    assign ready = bits_left == 0;
    assign tx = ready ? 1'b1 : shift[0];
    always @(posedge clk) begin
        if (reset) begin
            shift <= 10'h3ff;
            bits_left <= 0;
            count <= 0;
        end else if (ready) begin
            if (valid) begin
                shift <= {1'b1, data, 1'b0};
                bits_left <= 10;
                count <= CLKS_PER_BIT-1;
            end
        end else if (count != 0) begin
            count <= count-1;
        end else begin
            shift <= {1'b1, shift[9:1]};
            bits_left <= bits_left-1'b1;
            count <= CLKS_PER_BIT-1;
        end
    end
endmodule
