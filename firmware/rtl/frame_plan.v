`timescale 1ns/1ps
// Constants of a SEND for frame_sequencer, from the raw SEND fields.
// Combinational only. frame_controller registers the result in the control
// domain together with the SEND, so the core domain loads every constant
// as is: no adder, comparator or shifter lies between the two clocks, and
// none of this arithmetic runs at the core frequency.
//
// Field order, from the most significant bit (frame_sequencer unpacks the
// same list; PLAN_BITS is their total width). T = N+L+G is the tail step.
//   word[25:0]            first bit on bit 25, whatever the bit order
//   bits[4:0], bits==1, bits==2
//   N-1[15:0], N[15:0], N+1[16:0], N==1, N==2, N==3, N==4, N==5
//   N+L-1[16:0], N+L[16:0]
//   2N+1[17:0], T+1[17:0], T==2, T==3, T==4, T==5
//   repeat+1[16:0], repeat==0, repeat==1
//   flags[2:0]
module frame_plan #(
    // frame_controller aligns the word in its validation pipeline:
    // MSB-first words arrive left-aligned on bit 25, LSB-first words as is.
    parameter WORD_PREALIGNED = 0,
    parameter integer PLAN_BITS = 183
) (
    input wire [31:0] word_in,
    input wire [4:0] bits_in,
    input wire [15:0] divider_in, latch_ticks_in, gap_ticks_in, repeat_in,
    input wire [2:0] flags_in,
    output wire [PLAN_BITS-1:0] plan
);
    wire [25:0] aligned = WORD_PREALIGNED ? word_in[25:0]
        : (flags_in[0] ? word_in[25:0]
            : (word_in[25:0] << (5'd26 - bits_in)));
    // LSB first: reverse the word, so the sequencer always sends bit 25 and
    // shifts left.
    reg [25:0] word;
    integer index;
    always @* begin
        for (index=0; index<26; index=index+1)
            word[index] = flags_in[0] ? aligned[25-index] : aligned[index];
    end
    wire [16:0] divider = {1'b0, divider_in};
    wire [16:0] latch_end = divider + latch_ticks_in;
    wire [17:0] bit_ticks = {divider, 1'b0};
    wire [17:0] tail = latch_end + gap_ticks_in;
    assign plan = {
        word, bits_in, bits_in == 5'd1, bits_in == 5'd2,
        divider_in - 16'd1, divider_in, divider + 17'd1,
        divider_in == 16'd1, divider_in == 16'd2, divider_in == 16'd3,
        divider_in == 16'd4, divider_in == 16'd5,
        latch_end - 17'd1, latch_end,
        bit_ticks + 18'd1, tail + 18'd1,
        tail == 18'd2, tail == 18'd3, tail == 18'd4, tail == 18'd5,
        {1'b0, repeat_in} + 17'd1, repeat_in == 16'd0, repeat_in == 16'd1,
        flags_in
    };
endmodule
