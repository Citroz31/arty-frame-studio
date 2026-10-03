`timescale 1ns/1ps
// Arty A7-100T: 100 MHz board oscillator, FT2232 USB UART, three Pmod outputs.
// Pin constraints are kept separately in firmware/constraints/arty_a7_100t.xdc;
// arty_frame_studio.firmware_config generates them for custom builds.
module arty_top #(
    // CORE_HZ must equal 100 MHz * PLL_MULT / PLL_OUT_DIV (VCO 800-1600 MHz).
    parameter integer CORE_HZ=200000000,
    parameter integer PLL_MULT=10,
    parameter integer PLL_OUT_DIV=5,
    // 0 identifies the reference build; custom builds carry a config hash.
    parameter [31:0] BUILD_ID=32'h0,
    // A host LED command overrides the status LEDs for this many core cycles.
    parameter integer LED_HOLD_CYCLES=CORE_HZ*3
) (
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
    // An inconsistent parameter set instantiates an undefined module, so
    // both synthesis and simulation elaboration stop with its name.
    generate
        if (CORE_HZ * PLL_OUT_DIV != 100000000 * PLL_MULT
            || PLL_MULT < 8 || PLL_MULT > 16 || PLL_OUT_DIV < 1 || PLL_OUT_DIV > 128)
        begin: invalid_clock_parameters
            CORE_HZ_does_not_match_PLL_MULT_and_PLL_OUT_DIV invalid();
        end
        // The LED hold counter is 31 bits wide; its bit 30 marks expiry.
        if (LED_HOLD_CYCLES < 1 || LED_HOLD_CYCLES >= 32'h40000000)
        begin: invalid_led_hold
            LED_HOLD_CYCLES_must_be_between_1_and_2_pow_30 invalid();
        end
    endgenerate
    localparam [30:0] LED_HOLD = LED_HOLD_CYCLES;
    IBUF input_buffer (.I(clk100), .O(input_clock));
    // Integer PLL configuration is supported by the nextpnr-xilinx FASM
    // writer. Reference: VCO=100 MHz*10=1 GHz, CLKOUT0=1 GHz/5=200 MHz.
    // COMPENSATION is a PLLE2_ADV parameter; PLLE2_BASE does not expose it.
    PLLE2_ADV #(
        .BANDWIDTH("OPTIMIZED"), .COMPENSATION("INTERNAL"), .CLKIN1_PERIOD(10.0),
        .DIVCLK_DIVIDE(1), .CLKFBOUT_MULT(PLL_MULT),
        .CLKOUT0_DIVIDE(PLL_OUT_DIV), .STARTUP_WAIT("FALSE")
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
    wire led_write;
    wire [4:0] led_value;
    uart_rx #(.CLOCK_HZ(CORE_HZ), .BAUD(115200)) receiver (
        .clk(core_clock), .reset(reset), .rx(uart_rx),
        .data(received_byte), .valid(received_valid)
    );
    uart_tx #(.CLOCK_HZ(CORE_HZ), .BAUD(115200)) transmitter (
        .clk(core_clock), .reset(reset), .data(transmit_byte),
        .valid(transmit_valid), .ready(transmit_ready), .tx(uart_tx)
    );
    // An incomplete packet expires after 200 ms at any core frequency.
    frame_controller #(
        .PACKET_TIMEOUT_CYCLES(CORE_HZ / 5), .CORE_HZ(CORE_HZ), .BUILD_ID(BUILD_ID)
    ) controller (
        .clk(core_clock), .reset(reset),
        .rx_data(received_byte), .rx_valid(received_valid),
        .tx_data(transmit_byte), .tx_valid(transmit_valid),
        .tx_ready(transmit_ready), .busy(busy), .completed(completed),
        .data_rise(data_rise), .data_fall(data_fall),
        .clock_rise(clock_rise), .clock_fall(clock_fall),
        .latch_rise(latch_rise), .latch_fall(latch_fall),
        .led_write(led_write), .led_value(led_value)
    );
    // frame_engine decodes the falling-edge values combinationally. Register
    // all six ODDR inputs so only a flip-flop-to-OLOGIC route remains: the
    // nextpnr report does not certify the fabric-to-OLOGIC endpoints. The
    // uniform one-cycle delay keeps DATA, CLK and LATCH aligned.
    reg [5:0] oddr_inputs;
    always @(posedge core_clock) begin
        if (reset) oddr_inputs <= 0;
        else oddr_inputs <= {data_rise, data_fall, clock_rise, clock_fall,
                             latch_rise, latch_fall};
    end
    ODDR #(.DDR_CLK_EDGE("SAME_EDGE"), .INIT(1'b0), .SRTYPE("ASYNC")) data_ddr (
        .C(core_clock), .CE(1'b1), .D1(oddr_inputs[5]), .D2(oddr_inputs[4]),
        .R(reset), .S(1'b0), .Q(data_out)
    );
    ODDR #(.DDR_CLK_EDGE("SAME_EDGE"), .INIT(1'b0), .SRTYPE("ASYNC")) clock_ddr (
        .C(core_clock), .CE(1'b1), .D1(oddr_inputs[3]), .D2(oddr_inputs[2]),
        .R(reset), .S(1'b0), .Q(frame_clk)
    );
    ODDR #(.DDR_CLK_EDGE("SAME_EDGE"), .INIT(1'b0), .SRTYPE("ASYNC")) latch_ddr (
        .C(core_clock), .CE(1'b1), .D1(oddr_inputs[1]), .D2(oddr_inputs[0]),
        .R(reset), .S(1'b0), .Q(latch_enable)
    );
    // LED test: a manual LED command shows its pattern on LD4-LD7, then the
    // status display returns after LED_HOLD_CYCLES; an automatic command or
    // a reset returns at once. led_hold[30] set means status display.
    reg [3:0] led_pattern;
    reg [30:0] led_hold;
    always @(posedge core_clock) begin
        if (reset) begin
            led_pattern <= 0;
            led_hold <= {31{1'b1}};
        end else if (led_write) begin
            led_pattern <= led_value[3:0];
            led_hold <= led_value[4] ? LED_HOLD : {31{1'b1}};
        end else if (!led_hold[30]) begin
            led_hold <= led_hold - 1'b1;
        end
    end
    wire [3:0] status_leds = {completed != 0, 1'b0, busy, locked};
    assign led = led_hold[30] ? status_leds : led_pattern;
endmodule
