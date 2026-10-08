`timescale 1ns/1ps
// Arty A7-100T: 100 MHz board oscillator, FT2232 USB UART, four Pmod outputs.
// Pin constraints are kept separately in firmware/constraints/arty_a7_100t.xdc;
// arty_frame_studio.firmware_config generates them for custom builds.
//
// Two unrelated clock domains:
//  - ctrl_clock, the 100 MHz board oscillator: UART, packets, replies, LEDs,
//    TR. Its timing never depends on the frame frequency;
//  - core_clock, from the PLL: the frame engine and the output DDR registers
//    only. CLK = core_clock / divider, so this is the clock to raise for a
//    fast frame.
module arty_top #(
    // CORE_HZ = 100 MHz * PLL_MULT / (PLL_IN_DIV * PLL_OUT_DIV), rounded to the
    // hertz. Speed grade -1 limits: phase detector 100 MHz / PLL_IN_DIV within
    // 19-450 MHz, VCO 100 MHz * PLL_MULT / PLL_IN_DIV within 800-1600 MHz.
    parameter integer CORE_HZ=200000000,
    parameter integer PLL_MULT=10,
    parameter integer PLL_IN_DIV=1,
    parameter integer PLL_OUT_DIV=5,
    // 0 identifies the reference build; custom builds carry a config hash.
    parameter [31:0] BUILD_ID=32'h0,
    // A host LED command overrides the status LEDs for this many control
    // (100 MHz) cycles.
    parameter integer LED_HOLD_CYCLES=300000000
) (
    input wire clk100,
    input wire reset_n,
    input wire uart_rx,
    output wire uart_tx,
    output wire data_out,
    output wire frame_clk,
    output wire latch_enable,
    // Static transmit/receive level set by the host (3.3 V = 1, 0 V = 0).
    output wire tr_out,
    output wire [3:0] led
);
    localparam integer CTRL_HZ = 100000000;
    wire input_clock, ctrl_clock, feedback_raw, feedback_clock, clock_raw, core_clock;
    wire locked;
    // Exact clock arithmetic in 64 bits: CORE_HZ * PLL_IN_DIV * PLL_OUT_DIV
    // must round 100 MHz * PLL_MULT, i.e. differ by at most half the divisor.
    localparam [63:0] DIVISOR = 64'd1 * PLL_IN_DIV * PLL_OUT_DIV;
    localparam [63:0] SCALED_CORE = 64'd1 * CORE_HZ * DIVISOR;
    localparam [63:0] SCALED_VCO = 64'd100000000 * PLL_MULT;
    localparam [63:0] HALF_DIVISOR = DIVISOR / 2;
    // An inconsistent parameter set instantiates an undefined module, so
    // both synthesis and simulation elaboration stop with its name.
    generate
        if (PLL_MULT < 2 || PLL_MULT > 64 || PLL_IN_DIV < 1 || PLL_IN_DIV > 5
            || PLL_OUT_DIV < 1 || PLL_OUT_DIV > 128
            || PLL_MULT < 8 * PLL_IN_DIV || PLL_MULT > 16 * PLL_IN_DIV
            || SCALED_CORE + HALF_DIVISOR < SCALED_VCO
            || SCALED_CORE > SCALED_VCO + HALF_DIVISOR)
        begin: invalid_clock_parameters
            CORE_HZ_does_not_match_PLL_MULT_PLL_IN_DIV_and_PLL_OUT_DIV invalid();
        end
        // The LED hold counter is 31 bits wide; its bit 30 marks expiry.
        if (LED_HOLD_CYCLES < 1 || LED_HOLD_CYCLES >= 32'h40000000)
        begin: invalid_led_hold
            LED_HOLD_CYCLES_must_be_between_1_and_2_pow_30 invalid();
        end
    endgenerate
    localparam [30:0] LED_HOLD = LED_HOLD_CYCLES;
    IBUF input_buffer (.I(clk100), .O(input_clock));
    BUFG ctrl_buffer (.I(input_clock), .O(ctrl_clock));
    // Integer PLL configuration: nextpnr-xilinx writes the DIVCLK, CLKFBOUT and
    // CLKOUT0 counters and the lock/loop-filter tables harvested from Vivado
    // for every CLKFBOUT_MULT; arty_frame_studio checks them in the FASM.
    // Reference: VCO=100 MHz*10=1 GHz, CLKOUT0=1 GHz/5=200 MHz.
    // COMPENSATION is a PLLE2_ADV parameter; PLLE2_BASE does not expose it.
    PLLE2_ADV #(
        .BANDWIDTH("OPTIMIZED"), .COMPENSATION("INTERNAL"), .CLKIN1_PERIOD(10.0),
        .DIVCLK_DIVIDE(PLL_IN_DIV), .CLKFBOUT_MULT(PLL_MULT),
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
    // clocks of each domain. Output ODDRs receive the same asynchronous reset.
    // Both domains reset together, so the toggles of engine_link start equal.
    wire reset_async = !reset_n || !locked;
    (* ASYNC_REG="TRUE" *) reg [3:0] reset_pipe=4'hf;
    always @(posedge core_clock or posedge reset_async) begin
        if (reset_async) reset_pipe <= 4'hf;
        else reset_pipe <= {reset_pipe[2:0], 1'b0};
    end
    wire reset = reset_pipe[3];
    (* ASYNC_REG="TRUE" *) reg [3:0] ctrl_reset_pipe=4'hf;
    always @(posedge ctrl_clock or posedge reset_async) begin
        if (reset_async) ctrl_reset_pipe <= 4'hf;
        else ctrl_reset_pipe <= {ctrl_reset_pipe[2:0], 1'b0};
    end
    wire ctrl_reset = ctrl_reset_pipe[3];
    wire [7:0] received_byte, transmit_byte;
    wire received_valid, transmit_valid, transmit_ready;
    wire busy;
    wire [15:0] completed;
    wire data_rise, data_fall, clock_rise, clock_fall, latch_rise, latch_fall;
    wire led_write;
    wire [4:0] led_value;
    wire tr_write, tr_value;
    uart_rx #(.CLOCK_HZ(CTRL_HZ), .BAUD(115200)) receiver (
        .clk(ctrl_clock), .reset(ctrl_reset), .rx(uart_rx),
        .data(received_byte), .valid(received_valid)
    );
    uart_tx #(.CLOCK_HZ(CTRL_HZ), .BAUD(115200)) transmitter (
        .clk(ctrl_clock), .reset(ctrl_reset), .data(transmit_byte),
        .valid(transmit_valid), .ready(transmit_ready), .tx(uart_tx)
    );
    // An incomplete packet expires after 200 ms.
    frame_controller #(
        .PACKET_TIMEOUT_CYCLES(CTRL_HZ / 5), .CORE_HZ(CORE_HZ), .BUILD_ID(BUILD_ID)
    ) controller (
        .clk(ctrl_clock), .reset(ctrl_reset),
        .core_clk(core_clock), .core_reset(reset),
        .rx_data(received_byte), .rx_valid(received_valid),
        .tx_data(transmit_byte), .tx_valid(transmit_valid),
        .tx_ready(transmit_ready), .busy(busy), .completed(completed),
        .data_rise(data_rise), .data_fall(data_fall),
        .clock_rise(clock_rise), .clock_fall(clock_fall),
        .latch_rise(latch_rise), .latch_fall(latch_fall),
        .led_write(led_write), .led_value(led_value),
        .tr_write(tr_write), .tr_value(tr_value)
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
    // TR: a static level, no clock. The one-cycle write comes from the
    // reply-queue capacity logic: register it before it reaches the output
    // flip-flop, which sits near the distant pin. Reset returns the pin to 0 V.
    reg tr_command, tr_command_value, tr_level;
    always @(posedge ctrl_clock) begin
        tr_command <= !ctrl_reset && tr_write;
        tr_command_value <= tr_value;
        if (ctrl_reset) tr_level <= 1'b0;
        else if (tr_command) tr_level <= tr_command_value;
    end
    assign tr_out = tr_level;
    // LED test: a manual LED command shows its pattern on LD4-LD7, then the
    // status display returns after LED_HOLD_CYCLES; an automatic command or
    // a reset returns at once. led_hold[30] set means status display.
    // led_write comes from the reply-queue capacity logic: register it before
    // it fans out to the counter, which is placed near the distant LED pins.
    reg led_command;
    reg [4:0] led_command_value;
    reg [3:0] led_pattern;
    reg [30:0] led_hold;
    always @(posedge ctrl_clock) begin
        led_command <= !ctrl_reset && led_write;
        led_command_value <= led_value;
        if (ctrl_reset) begin
            led_pattern <= 0;
            led_hold <= {31{1'b1}};
        end else if (led_command) begin
            led_pattern <= led_command_value[3:0];
            led_hold <= led_command_value[4] ? LED_HOLD : {31{1'b1}};
        end else if (!led_hold[30]) begin
            led_hold <= led_hold - 1'b1;
        end
    end
    wire [3:0] status_leds = {completed != 0, 1'b0, busy, locked};
    assign led = led_hold[30] ? status_leds : led_pattern;
endmodule
