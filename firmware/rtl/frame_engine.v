`timescale 1ns/1ps
// Frame engine with the raw SEND interface, in one clock domain: frame_plan
// prepares the constants combinationally and frame_sequencer plays them.
// Two logical ticks per core cycle: connect *_rise/*_fall to ODDR D1/D2
// with DDR_CLK_EDGE="SAME_EDGE". See frame_sequencer for the waveform.
//
// The board build does not use this wrapper: frame_controller registers the
// plan in its own (control) clock domain and drives frame_sequencer on the
// core clock. Simulation benches use it to check the sequencer against the
// raw SEND fields.
module frame_engine #(
    // The word arrives already aligned: MSB-first words on bit 25, LSB-first
    // words as is. The default aligns it from bits_in.
    parameter WORD_PREALIGNED = 0
) (
    input wire clk, input wire reset,
    input wire start, input wire stop,
    input wire [31:0] word_in,
    input wire [4:0] bits_in,
    input wire [15:0] divider_in, latch_ticks_in, gap_ticks_in, repeat_in,
    input wire [2:0] flags_in,
    output wire busy,
    output wire [15:0] completed,
    output wire data_rise, data_fall,
    output wire clock_rise, clock_fall,
    output wire latch_rise, latch_fall
);
    wire [182:0] plan;
    frame_plan #(.WORD_PREALIGNED(WORD_PREALIGNED), .PLAN_BITS(183)) planner (
        .word_in(word_in), .bits_in(bits_in), .divider_in(divider_in),
        .latch_ticks_in(latch_ticks_in), .gap_ticks_in(gap_ticks_in),
        .repeat_in(repeat_in), .flags_in(flags_in), .plan(plan)
    );
    frame_sequencer #(.PLAN_BITS(183)) sequencer (
        .clk(clk), .reset(reset), .start(start), .stop(stop), .plan(plan),
        .busy(busy), .completed(completed),
        .data_rise(data_rise), .data_fall(data_fall),
        .clock_rise(clock_rise), .clock_fall(clock_fall),
        .latch_rise(latch_rise), .latch_fall(latch_fall)
    );
endmodule
