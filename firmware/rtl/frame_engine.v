`timescale 1ns/1ps
// Two logical 2.5 ns steps per 200 MHz cycle. Connect *_rise/*_fall to
// ODDR D1/D2 with DDR_CLK_EDGE="SAME_EDGE". No 400 MHz fabric clock.
// The two boundaries are decoded in parallel: no cascaded half-step ALUs.
// repeat_in=0 repeats the frame continuously until STOP; completed then
// counts finished frames modulo 2^16.
// flags_in[2] (free CLK): CLK keeps its 2*divider period through LATCH and
// the pause, from SEND to the end. LATCH then starts at the last falling
// edge of the bits. The host sends latch_ticks=(2k-1)*divider and
// gap_ticks=2m*divider, so every frame lasts whole CLK periods and each
// frame starts in phase with CLK: DATA and LATCH change on falling edges.
module frame_engine #(
    // A packet controller can align the word in its validation pipeline.
    // The default keeps the standalone SEND interface unchanged.
    parameter WORD_PREALIGNED = 0
) (
    input wire clk, input wire reset,
    input wire start, input wire stop,
    input wire [31:0] word_in,
    input wire [4:0] bits_in,
    input wire [15:0] divider_in, latch_ticks_in, gap_ticks_in, repeat_in,
    input wire [2:0] flags_in,
    output wire busy,
    output reg [15:0] completed,
    output reg data_rise, data_fall,
    output reg clock_rise, clock_fall,
    output reg latch_rise, latch_fall
);
    localparam [5:0] IDLE=6'b000001, RISE_WAIT=6'b000010,
        FALL_WAIT=6'b000100, PRE_LATCH=6'b001000,
        LATCH_HOLD=6'b010000, GAP_WAIT=6'b100000;
    reg [5:0] phase, next_phase;
    reg [25:0] word_config, shift_word;
    reg [4:0] bit_count, bits_left;
    reg [15:0] divider, latch_ticks, gap_ticks, remaining, ticks;
    reg [15:0] divider_minus_one, latch_minus_one, gap_minus_one;
    reg [2:0] flags;
    reg initial_data, data_level, clock_level, latch_level;
    reg continuous, last_frame;
    // Free-running CLK generator: level, ticks to the next edge, and the
    // registered ticks==1/ticks==2 decodes, as for the frame timer.
    reg [15:0] free_ticks;
    reg free_level, free_one, free_two;
    reg ticks_one, ticks_two;
    reg divider_one, divider_two, divider_three;
    reg latch_one, latch_two, latch_three;
    reg gap_zero, gap_one, gap_two, gap_three;
    reg next_data, next_clock, next_latch;
    reg reload_divider, reload_latch, reload_gap, reload_minus_one;
    reg shift_data, finish;

    // Alignment occurs only at SEND. During transmission the next data bit
    // is a fixed wire, avoiding an index adder and a 32:1 mux on each edge.
    wire [25:0] aligned_input = WORD_PREALIGNED ? word_in[25:0]
        : (flags_in[0] ? word_in[25:0]
            : (word_in[25:0] << (5'd26 - bits_in)));
    wire input_data = flags_in[0] ? aligned_input[0] : aligned_input[25];
    wire following_data = flags[0] ? shift_word[1] : shift_word[24];
    wire [25:0] shifted_word = flags[0] ? {1'b0,shift_word[25:1]}
        : {shift_word[24:0],1'b0};
    wire last_bit = bits_left == 1;
    // Free CLK: the last falling edge of the bits also starts LATCH.
    wire free_latch = flags[2] && last_bit;
    // last_frame is a register equal to (remaining == 1 && !continuous),
    // maintained wherever remaining changes. The 16-bit comparison stays
    // off the finish/next-state path; a continuous SEND never sets it.
    wire boundary = ticks_one || ticks_two;
    wire first_finish = ticks_one && (phase[5] || (phase[4] && gap_zero));
    // All assignments use the explicit one-hot constants above. The IDLE
    // bit therefore gives busy directly, without a wide state comparator
    // in the feedback path to SEND acceptance and register enables.
    assign busy = !phase[0];

    always @* begin
        next_data = data_level;
        next_clock = clock_level;
        next_latch = latch_level;
        data_rise = data_level;
        clock_rise = clock_level;
        latch_rise = latch_level;
        data_fall = data_level;
        clock_fall = clock_level;
        latch_fall = latch_level;
        reload_divider = 0;
        reload_latch = 0;
        reload_gap = 0;
        reload_minus_one = ticks_one;
        shift_data = 0;
        // A core cycle spans at most two boundaries, so at most one frame
        // can finish. Decode all finish routes in parallel from the old state.
        finish = (phase[5] && boundary)
            || (phase[4] && boundary && gap_zero)
            || (phase[4] && ticks_one && gap_one)
            || (phase[3] && ticks_one && latch_one && gap_zero);

        // Independent one-hot next-state equations avoid a cascaded phase
        // selection followed by a second mux for frame completion/repetition.
        next_phase[0] = phase[0] || (finish && last_frame);
        next_phase[1] = (phase[1] && !boundary)
            || (phase[1] && ticks_one && divider_one && !last_bit)
            || (phase[2] && boundary && !last_bit
                && !(ticks_one && divider_one))
            || (finish && !last_frame && !(first_finish && divider_one));
        next_phase[2] = (phase[2] && !boundary)
            || (phase[2] && ticks_one && divider_one && !last_bit)
            || (phase[1] && boundary && !(ticks_one && divider_one))
            || (finish && !last_frame && first_finish && divider_one);
        next_phase[3] = (phase[3] && !boundary)
            || (phase[2] && boundary && last_bit
                && !(ticks_one && divider_one))
            || (phase[1] && ticks_one && divider_one && last_bit);
        next_phase[4] = (phase[4] && !boundary)
            || (phase[3] && boundary && !(ticks_one && latch_one))
            || (phase[2] && ticks_one && divider_one && last_bit);
        next_phase[5] = (phase[5] && !boundary)
            || (phase[4] && boundary && !gap_zero && !(ticks_one && gap_one))
            || (phase[3] && ticks_one && latch_one && !gap_zero);

        if (boundary) begin
            // The state register is one-hot on reset and every transition.
            // Decode its bits directly and preserve that mutual exclusivity
            // in synthesis instead of rebuilding six-bit comparators.
            (* parallel_case *) case (1'b1)
                phase[1]: begin
                    next_clock = 1;
                    reload_divider = 1;
                    if (ticks_one) begin
                        clock_fall = 1;
                        if (divider_one) begin
                            // Both rise and fall occur in this core cycle.
                            next_clock = 0;
                            next_data = last_bit ? 1'b0 : following_data;
                            shift_data = !last_bit;
                            reload_minus_one = 0;
                            // Free CLK: LATCH from the last falling edge.
                            if (free_latch) next_latch = !flags[1];
                        end
                    end
                end
                phase[2]: begin
                    next_clock = 0;
                    next_data = last_bit ? 1'b0 : following_data;
                    shift_data = !last_bit;
                    reload_divider = 1;
                    if (free_latch) next_latch = !flags[1];
                    if (ticks_one) begin
                        clock_fall = 0;
                        data_fall = next_data;
                        if (free_latch) latch_fall = !flags[1];
                        if (divider_one) begin
                            reload_minus_one = 0;
                            if (last_bit) begin
                                next_latch = !flags[1];
                                reload_divider = 0;
                                reload_latch = 1;
                            end else begin
                                next_clock = 1;
                            end
                        end
                    end
                end
                phase[3]: begin
                    next_latch = !flags[1];
                    reload_latch = 1;
                    if (ticks_one) begin
                        latch_fall = !flags[1];
                        if (latch_one) begin
                            next_latch = flags[1];
                            reload_latch = 0;
                            reload_minus_one = 0;
                            if (!gap_zero) begin
                                reload_gap = 1;
                            end
                        end
                    end
                end
                phase[4]: begin
                    next_latch = flags[1];
                    if (ticks_one) latch_fall = flags[1];
                    if (!gap_zero) begin
                        reload_gap = 1;
                        if (ticks_one && gap_one) begin
                            reload_gap = 0;
                            reload_minus_one = 0;
                        end
                    end
                end
                default: begin end
            endcase

            if (finish) begin
                next_clock = 0;
                next_data = last_frame ? 1'b0 : initial_data;
                reload_divider = !last_frame;
                // A direct finish on the first boundary leaves one half-tick
                // for the new repetition. Other finishes are the second edge.
                if (first_finish) begin
                    data_fall = next_data;
                    clock_fall = 0;
                    if (!last_frame && divider_one) begin
                        next_clock = 1;
                        reload_minus_one = 0;
                    end
                end else reload_minus_one = 0;
            end
        end

        // Free CLK replaces the gated CLK while a sequence runs. Both start
        // low at SEND with the same period, so they agree during the bits.
        if (flags[2] && busy) begin
            clock_rise = free_level;
            clock_fall = free_one ? !free_level : free_level;
        end
    end

    always @(posedge clk) begin
        if (reset) begin
            phase <= IDLE;
            word_config <= 0;
            shift_word <= 0;
            bit_count <= 1;
            bits_left <= 1;
            divider <= 1;
            divider_minus_one <= 0;
            latch_ticks <= 1;
            latch_minus_one <= 0;
            gap_ticks <= 0;
            gap_minus_one <= 0;
            remaining <= 0;
            continuous <= 0;
            last_frame <= 0;
            free_ticks <= 0;
            free_level <= 0;
            free_one <= 0;
            free_two <= 0;
            ticks <= 0;
            ticks_one <= 0;
            ticks_two <= 0;
            divider_one <= 1;
            divider_two <= 0;
            divider_three <= 0;
            latch_one <= 1;
            latch_two <= 0;
            latch_three <= 0;
            gap_zero <= 1;
            gap_one <= 0;
            gap_two <= 0;
            gap_three <= 0;
            flags <= 0;
            completed <= 0;
            initial_data <= 0;
            data_level <= 0;
            clock_level <= 0;
            latch_level <= 0;
        end else if (stop) begin
            phase <= IDLE;
            remaining <= 0;
            last_frame <= 0;
            free_level <= 0;
            free_one <= 0;
            free_two <= 0;
            ticks_one <= 0;
            ticks_two <= 0;
            data_level <= 0;
            clock_level <= 0;
            latch_level <= flags[1];
        end else if (start && !busy) begin
            word_config <= aligned_input;
            shift_word <= aligned_input;
            bit_count <= bits_in;
            bits_left <= bits_in;
            divider <= divider_in;
            divider_minus_one <= divider_in - 1'b1;
            latch_ticks <= latch_ticks_in;
            latch_minus_one <= latch_ticks_in - 1'b1;
            gap_ticks <= gap_ticks_in;
            gap_minus_one <= gap_ticks_in - 1'b1;
            remaining <= repeat_in;
            continuous <= repeat_in == 0;
            last_frame <= repeat_in == 1;
            free_ticks <= divider_in;
            free_level <= 0;
            free_one <= divider_in == 1;
            free_two <= divider_in == 2;
            ticks <= divider_in;
            ticks_one <= divider_in == 1;
            ticks_two <= divider_in == 2;
            divider_one <= divider_in == 1;
            divider_two <= divider_in == 2;
            divider_three <= divider_in == 3;
            latch_one <= latch_ticks_in == 1;
            latch_two <= latch_ticks_in == 2;
            latch_three <= latch_ticks_in == 3;
            gap_zero <= gap_ticks_in == 0;
            gap_one <= gap_ticks_in == 1;
            gap_two <= gap_ticks_in == 2;
            gap_three <= gap_ticks_in == 3;
            flags <= flags_in;
            completed <= 0;
            phase <= RISE_WAIT;
            initial_data <= input_data;
            data_level <= input_data;
            clock_level <= 0;
            latch_level <= flags_in[1];
        end else begin
            phase <= next_phase;
            data_level <= next_data;
            clock_level <= next_clock;
            latch_level <= next_latch;
            if (finish) begin
                completed <= completed + 1'b1;
                // A continuous count wraps freely and never ends the emission.
                remaining <= remaining - 1'b1;
                last_frame <= !continuous && remaining == 2;
                bits_left <= bit_count;
                shift_word <= word_config;
            end else if (shift_data) begin
                bits_left <= bits_left - 1'b1;
                shift_word <= shifted_word;
            end
            // Every reload belongs to an active one-hot phase. Put that
            // invariant around the timer selection so its register enable
            // depends on busy/SEND, not the complete reload decision tree.
            if (busy) begin
                if (reload_divider) begin
                    ticks <= reload_minus_one ? divider_minus_one : divider;
                    ticks_one <= reload_minus_one ? divider_two : divider_one;
                    ticks_two <= reload_minus_one ? divider_three : divider_two;
                end else if (reload_latch) begin
                    ticks <= reload_minus_one ? latch_minus_one : latch_ticks;
                    ticks_one <= reload_minus_one ? latch_two : latch_one;
                    ticks_two <= reload_minus_one ? latch_three : latch_two;
                end else if (reload_gap) begin
                    ticks <= reload_minus_one ? gap_minus_one : gap_ticks;
                    ticks_one <= reload_minus_one ? gap_two : gap_one;
                    ticks_two <= reload_minus_one ? gap_three : gap_two;
                end else begin
                    ticks <= ticks - 2'd2;
                    ticks_one <= ticks == 3;
                    ticks_two <= ticks == 4;
                end
            end else begin
                ticks_one <= 0;
                ticks_two <= 0;
            end
            // One CLK edge every divider ticks, two ticks per core cycle.
            // An edge on the first boundary is reloaded with divider-1; with
            // divider 1 both boundaries toggle and the level is unchanged.
            if (!busy) begin
                free_level <= 0;
                free_one <= 0;
                free_two <= 0;
            end else if (free_one) begin
                if (!divider_one) begin
                    free_level <= !free_level;
                    free_ticks <= divider_minus_one;
                    free_one <= divider_two;
                    free_two <= divider_three;
                end
            end else if (free_two) begin
                free_level <= !free_level;
                free_ticks <= divider;
                free_one <= divider_one;
                free_two <= divider_two;
            end else begin
                free_ticks <= free_ticks - 2'd2;
                free_one <= free_ticks == 3;
                free_two <= free_ticks == 4;
            end
        end
    end
endmodule
