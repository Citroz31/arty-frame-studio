`timescale 1ns/1ps
// Arty A7-100T: 100 MHz board oscillator, FT2232 USB UART, three JB outputs.
// Pin constraints are kept separately in firmware/constraints/arty_a7_100t.xdc.
module arty_top (
    input wire clk100,
    input wire reset_n,
    input wire uart_rx,
    output wire uart_tx,
    output wire data_out,
    output wire frame_clk,
    output wire latch_enable,
    output wire [3:0] led
);
    wire input_clock, feedback_raw, feedback_clock, clock_raw, core_clock;
    wire locked;
    IBUF input_buffer (.I(clk100), .O(input_clock));
    // Integer PLL configuration is supported by the nextpnr-xilinx FASM
    // writer: VCO=100 MHz*10=1 GHz, CLKOUT0=1 GHz/5=200 MHz.
    // COMPENSATION is a PLLE2_ADV parameter; PLLE2_BASE does not expose it.
    PLLE2_ADV #(
        .BANDWIDTH("OPTIMIZED"), .COMPENSATION("INTERNAL"), .CLKIN1_PERIOD(10.0),
        .DIVCLK_DIVIDE(1), .CLKFBOUT_MULT(10),
        .CLKOUT0_DIVIDE(5), .STARTUP_WAIT("FALSE")
    ) pll (
        .CLKIN1(input_clock), .CLKIN2(1'b0), .CLKINSEL(1'b1),
        .CLKFBIN(feedback_clock),
        .CLKFBOUT(feedback_raw), .CLKOUT0(clock_raw), .LOCKED(locked),
        .RST(!reset_n), .PWRDWN(1'b0),
        .DCLK(1'b0), .DEN(1'b0), .DWE(1'b0),
        .DI(16'b0), .DADDR(7'b0), .DO(), .DRDY(),
        .CLKOUT1(), .CLKOUT2(), .CLKOUT3(), .CLKOUT4(), .CLKOUT5()
    );
    BUFG feedback_buffer (.I(feedback_raw), .O(feedback_clock));
    BUFG core_buffer (.I(clock_raw), .O(core_clock));

    // Assert reset without a running PLL clock; release only after 4 stable
    // core clocks. Output ODDRs receive the same asynchronous reset.
    wire reset_async = !reset_n || !locked;
    (* ASYNC_REG="TRUE" *) reg [3:0] reset_pipe=4'hf;
    always @(posedge core_clock or posedge reset_async) begin
        if (reset_async) reset_pipe <= 4'hf;
        else reset_pipe <= {reset_pipe[2:0], 1'b0};
    end
    wire reset = reset_pipe[3];
    wire [7:0] received_byte, transmit_byte;
    wire received_valid, transmit_valid, transmit_ready;
    wire busy;
    wire [15:0] completed;
    wire data_rise, data_fall, clock_rise, clock_fall, latch_rise, latch_fall;
    uart_rx #(.CLOCK_HZ(200000000), .BAUD(115200)) receiver (
        .clk(core_clock), .reset(reset), .rx(uart_rx),
        .data(received_byte), .valid(received_valid)
    );
    uart_tx #(.CLOCK_HZ(200000000), .BAUD(115200)) transmitter (
        .clk(core_clock), .reset(reset), .data(transmit_byte),
        .valid(transmit_valid), .ready(transmit_ready), .tx(uart_tx)
    );
    frame_controller controller (
        .clk(core_clock), .reset(reset),
        .rx_data(received_byte), .rx_valid(received_valid),
        .tx_data(transmit_byte), .tx_valid(transmit_valid),
        .tx_ready(transmit_ready), .busy(busy), .completed(completed),
        .data_rise(data_rise), .data_fall(data_fall),
        .clock_rise(clock_rise), .clock_fall(clock_fall),
        .latch_rise(latch_rise), .latch_fall(latch_fall)
    );
    ODDR #(.DDR_CLK_EDGE("SAME_EDGE"), .INIT(1'b0), .SRTYPE("ASYNC")) data_ddr (
        .C(core_clock), .CE(1'b1), .D1(data_rise), .D2(data_fall),
        .R(reset), .S(1'b0), .Q(data_out)
    );
    ODDR #(.DDR_CLK_EDGE("SAME_EDGE"), .INIT(1'b0), .SRTYPE("ASYNC")) clock_ddr (
        .C(core_clock), .CE(1'b1), .D1(clock_rise), .D2(clock_fall),
        .R(reset), .S(1'b0), .Q(frame_clk)
    );
    ODDR #(.DDR_CLK_EDGE("SAME_EDGE"), .INIT(1'b0), .SRTYPE("ASYNC")) latch_ddr (
        .C(core_clock), .CE(1'b1), .D1(latch_rise), .D2(latch_fall),
        .R(reset), .S(1'b0), .Q(latch_enable)
    );
    assign led = {completed != 0, 1'b0, busy, locked};
endmodule
