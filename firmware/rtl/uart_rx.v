`timescale 1ns/1ps
module uart_rx #(
    parameter integer CLOCK_HZ=200000000,
    parameter integer BAUD=115200
) (
    input wire clk, reset, rx,
    output reg [7:0] data,
    output reg valid
);
    localparam integer CLKS_PER_BIT=(CLOCK_HZ + BAUD/2)/BAUD;
    localparam integer COUNT_WIDTH=CLKS_PER_BIT <= 2 ? 1 : $clog2(CLKS_PER_BIT);
    localparam IDLE=0, START=1, DATA=2, STOP=3;
    (* ASYNC_REG="TRUE" *) reg rx_meta=1, rx_sync=1;
    reg [1:0] state;
    reg [COUNT_WIDTH-1:0] count;
    reg [2:0] index;
    reg [7:0] shift;
    always @(posedge clk) begin
        rx_meta <= rx;
        rx_sync <= rx_meta;
        if (reset) begin
            state <= IDLE;
            count <= 0;
            index <= 0;
            shift <= 0;
            data <= 0;
            valid <= 0;
        end else begin
            valid <= 0;
            case (state)
                IDLE: if (!rx_sync) begin
                    state <= START;
                    count <= CLKS_PER_BIT/2-1;
                end
                START: if (count != 0) count <= count-1;
                       else if (!rx_sync) begin
                           state <= DATA;
                           count <= CLKS_PER_BIT-1;
                           index <= 0;
                       end else state <= IDLE;
                DATA: if (count != 0) count <= count-1;
                      else begin
                          shift[index] <= rx_sync;
                          count <= CLKS_PER_BIT-1;
                          if (index == 7) state <= STOP;
                          else index <= index+1'b1;
                      end
                STOP: if (count != 0) count <= count-1;
                      else begin
                          if (rx_sync) begin
                              data <= shift;
                              valid <= 1;
                          end
                          state <= IDLE;
                      end
                default: state <= IDLE;
            endcase
        end
    end
endmodule
