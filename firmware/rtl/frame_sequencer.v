`timescale 1ns/1ps
// Plays the frames of a frame_plan, two logical ticks per core cycle.
// Connect *_rise/*_fall to ODDR D1/D2 with DDR_CLK_EDGE="SAME_EDGE":
// *_rise is the first tick of the cycle, *_fall the second.
//
// A frame is a list of steps. Each data bit is one BIT step of 2N ticks:
// DATA holds the bit, CLK is low for N ticks then high for N ticks. The
// frame ends with one TAIL step of T=N+L+G ticks: N ticks after the last
// falling edge, LATCH for L ticks, then the pause of G ticks. Every step
// lasts at least two ticks (N>=1, L>=1), so at most one step boundary falls
// in a core cycle, and it is known one cycle ahead (step_one, step_two).
//
// START arms the sequencer for one core cycle: busy, no step, outputs
// idle. That cycle acts as the end of a step on its second tick, so the
// first bit starts through the ordinary step change: the step logic never
// depends on START, STOP or reset, only the control state does.
//
// Every level is the sign bit of a down-counter, decremented by two per
// cycle and reloaded when a step starts; with e the ticks elapsed in the
// step on the first tick of the cycle:
//   step_ticks = len - e            ticks left in the step;
//   clock_count = N - 1 - e         < 0: CLK high in BIT, LATCH begun in TAIL;
//   latch_count = N + L - 1 - e     >= 0: LATCH not yet over in TAIL;
// and the same counters minus one for the second tick. A step starting on
// the next first tick reloads one more than a step that started on the
// second tick, so each counter has a single reload value K:
//   next = (reload ? K : count) - 2 + late
// computed as {-1...-1, late} + (reload ? K : count). The multiplexer is the
// LUT in front of each carry S input (3 inputs, 4 on bit 0) and the carry
// chain writes the register: no logic follows an adder, no comparator
// exists. nextpnr-xilinx moves an S LUT into the carry slice only up to four
// inputs; a wider one gets a feed-through LUT and a detour. The constant
// first operand makes Yosys feed the carry DI inputs with it (DI = late on
// bit 0). Equality decodes (ticks left == 3 or 4) are registered one cycle
// ahead.
//
// repeat=0 repeats the frame continuously until STOP; completed then counts
// finished frames modulo 2^16.
// flags[2] (free CLK): CLK keeps its 2N period through LATCH and the pause,
// from the first bit to the end. LATCH then starts at the last falling edge
// of the bits. The host sends L=(2k-1)*N and G=2m*N, so every frame lasts
// whole CLK periods and starts in phase with CLK: DATA and LATCH change on
// falling edges.
module frame_sequencer #(
    parameter integer PLAN_BITS = 183
) (
    input wire clk, input wire reset,
    input wire start, input wire stop,
    // frame_plan output: read while idle, including the START cycle.
    input wire [PLAN_BITS-1:0] plan,
    output wire busy,
    output reg [15:0] completed,
    output reg data_rise, data_fall,
    output reg clock_rise, clock_fall,
    output reg latch_rise, latch_fall
);
    wire [25:0] plan_word;
    wire [4:0] plan_bits;
    wire plan_bits_one, plan_bits_two;
    wire [15:0] plan_divider_minus_one, plan_divider;
    wire [16:0] plan_divider_plus_one;
    wire plan_divider_one, plan_divider_two, plan_divider_three;
    wire plan_divider_four, plan_divider_five;
    wire [16:0] plan_latch_end_minus_one, plan_latch_end;
    wire [17:0] plan_bit_plus_one, plan_tail_plus_one;
    wire plan_tail_two, plan_tail_three, plan_tail_four, plan_tail_five;
    wire [16:0] plan_repeat_plus_one;
    wire plan_continuous, plan_single;
    wire [2:0] plan_flags;
    assign {
        plan_word, plan_bits, plan_bits_one, plan_bits_two,
        plan_divider_minus_one, plan_divider, plan_divider_plus_one,
        plan_divider_one, plan_divider_two, plan_divider_three,
        plan_divider_four, plan_divider_five,
        plan_latch_end_minus_one, plan_latch_end,
        plan_bit_plus_one, plan_tail_plus_one,
        plan_tail_two, plan_tail_three, plan_tail_four, plan_tail_five,
        plan_repeat_plus_one, plan_continuous, plan_single,
        plan_flags
    } = plan;

    // Constants of the running SEND: they follow the plan while idle and
    // hold it from the START cycle on.
    reg [25:0] word_config;
    reg [4:0] bit_count;
    reg bits_one, bits_two;
    reg [15:0] divider_minus_one, divider;
    reg [16:0] divider_plus_one;
    reg divider_one, divider_two, divider_three, divider_four, divider_five;
    reg [16:0] latch_end_minus_one, latch_end;
    reg [17:0] bit_plus_one, tail_plus_one;
    reg tail_two, tail_three, tail_four, tail_five;
    reg continuous;
    reg [2:0] flags;

    // Control: run is busy (armed or playing), idle its complement; the step
    // type is clear while armed and idle.
    reg run, idle, bit_step, tail_step;
    // The current step ends after the first (step_one) or the second
    // (step_two) tick of this cycle; step_end is either. Idle holds step_two:
    // the cycle after START then starts the first bit on its first tick.
    reg step_one, step_two, step_end;
    // step_ticks == 3 and == 4, registered one cycle ahead.
    reg ticks_three, ticks_four;
    reg [17:0] step_ticks;
    // Length of the step after the current one, plus one, with its ==2..==5
    // decodes. While idle: the first bit.
    reg [17:0] next_plus_one;
    reg next_two, next_three, next_four, next_five;
    // Level counters (two's complement, sign bit 18) for the first and the
    // second tick of the cycle.
    reg [18:0] clock_count, clock_count_fall, latch_count, latch_count_fall;
    // Bits left in the frame, current one included, with ==1/==2 decodes.
    reg [4:0] bits_left;
    reg last_bit, second_last_bit;
    // The current bit is shift_word[25]; during TAIL it holds the next frame.
    reg [25:0] shift_word;
    // last_frame is a register equal to (remaining == 1 && !continuous),
    // maintained wherever remaining changes. A continuous SEND never sets it.
    reg [15:0] remaining;
    reg last_frame;
    // Free-running CLK generator: level, ticks to the next edge, and the
    // registered ticks==1..4 decodes, as for the step timer, with the reload
    // selects of its counter. Idle holds an edge on the second tick at high
    // level: the first bit starts low.
    reg [15:0] free_ticks;
    reg free_level, free_one, free_two, free_three, free_four;
    reg free_reload, free_late;

    assign busy = run;
    wire load = start && !run;
    wire finish = tail_step && step_end;
    wire run_next = !reset && !stop && (run ? !(finish && last_frame) : start);
    // Length of the step after the next one: TAIL if the next step is the
    // last bit. From TAIL or the START cycle, the next step is a first bit.
    wire after_next_tail = bit_step ? second_last_bit : bits_one;
    wire free = flags[2];
    wire latch_active = !flags[1];

    wire step_one_next = run && (step_one ? next_two : !step_two && ticks_three);
    wire step_two_next = !run
        || (step_one ? next_three : step_two ? next_two : ticks_four);
    // One copy of the reload selects per counter: each drives the
    // multiplexers of one carry chain only. Instantiated and kept, so
    // synthesis does not merge them back into one high-fanout pair. Copies
    // of run and step_end serve the wide register groups the same way:
    // run_for[0] the bits, [1] the next step, [2] the frame count and the
    // free CLK; end_for[0] the bits, [1] the next step.
    wire [4:0] reload, late;
    wire [2:0] run_for;
    wire [1:0] end_for;
    genvar copy;
    generate
        for (copy=0; copy<5; copy=copy+1) begin: select_copy
            (* keep *) FDRE #(.INIT(1'b1)) reload_copy (
                .C(clk), .CE(1'b1), .R(1'b0), .D(step_one_next || step_two_next),
                .Q(reload[copy])
            );
            (* keep *) FDRE #(.INIT(1'b1)) late_copy (
                .C(clk), .CE(1'b1), .R(1'b0), .D(step_two_next), .Q(late[copy])
            );
        end
        for (copy=0; copy<3; copy=copy+1) begin: run_copy
            (* keep *) FDRE #(.INIT(1'b0)) run_ff (
                .C(clk), .CE(1'b1), .R(1'b0), .D(run_next), .Q(run_for[copy])
            );
        end
        for (copy=0; copy<2; copy=copy+1) begin: end_copy
            (* keep *) FDRE #(.INIT(1'b1)) end_ff (
                .C(clk), .CE(1'b1), .R(1'b0), .D(step_one_next || step_two_next),
                .Q(end_for[copy])
            );
        end
    endgenerate

    // Counter step: reloaded with K - 2 when the next step started on the
    // second tick (one tick elapsed at the next first tick), K - 1 when it
    // starts on the next first tick, otherwise minus two.
    function [18:0] count;
        input reload_value, late_start;
        input [18:0] counting, value;
        count = {18'h3ffff, late_start} + (reload_value ? value : counting);
    endfunction
    // Free CLK edge on the first tick: N-1 ticks to the next one; on the
    // second tick: N. With N=1 the value is unused.
    wire free_one_next = run
        && (free_one ? divider_one || divider_two : free_two ? divider_one : free_three);
    wire free_two_next = !run
        || (free_one ? !divider_one && divider_three : free_two ? divider_two : free_four);

    always @* begin
        data_rise = bit_step && shift_word[25];
        clock_rise = bit_step && clock_count[18];
        latch_rise = tail_step && !latch_count[18] && (free || clock_count[18])
            ? latch_active : flags[1];
        if (step_one) begin
            // The step ended after the first tick: the second tick is the
            // first one of the next step. A bit starts with CLK low; TAIL
            // starts before LATCH, or with LATCH for free CLK.
            data_fall = (bit_step && !last_bit && shift_word[24])
                || (tail_step && !last_frame && shift_word[25]);
            clock_fall = 0;
            latch_fall = bit_step && last_bit && free ? latch_active : flags[1];
        end else begin
            data_fall = bit_step && shift_word[25];
            clock_fall = bit_step && clock_count_fall[18];
            latch_fall = tail_step && !latch_count_fall[18]
                && (free || clock_count_fall[18]) ? latch_active : flags[1];
        end
        // Free CLK replaces the gated CLK while frames play. Both start low
        // with the first bit and have the same period, so they agree during
        // the bits.
        if (free && (bit_step || tail_step)) begin
            clock_rise = free_level;
            clock_fall = free_one ? !free_level : free_level;
        end
    end

    // Constants of the SEND: a register enable and the plan bit, nothing
    // else. Only the LATCH polarity matters while idle: it changes with START
    // alone, is reset, and a STOP on the START cycle keeps the previous one.
    always @(posedge clk) begin
        if (reset) flags <= 0;
        else if (load && !stop) flags <= plan_flags;
    end
    always @(posedge clk) begin
        if (idle) begin
            word_config <= plan_word;
            bit_count <= plan_bits;
            bits_one <= plan_bits_one;
            bits_two <= plan_bits_two;
            divider_minus_one <= plan_divider_minus_one;
            divider <= plan_divider;
            divider_plus_one <= plan_divider_plus_one;
            divider_one <= plan_divider_one;
            divider_two <= plan_divider_two;
            divider_three <= plan_divider_three;
            divider_four <= plan_divider_four;
            divider_five <= plan_divider_five;
            latch_end_minus_one <= plan_latch_end_minus_one;
            latch_end <= plan_latch_end;
            bit_plus_one <= plan_bit_plus_one;
            tail_plus_one <= plan_tail_plus_one;
            tail_two <= plan_tail_two;
            tail_three <= plan_tail_three;
            tail_four <= plan_tail_four;
            tail_five <= plan_tail_five;
            continuous <= plan_continuous;
        end
    end

    // Control state: START, STOP and reset act on these registers only.
    always @(posedge clk) begin
        run <= run_next;
        idle <= !run_next;
        if (reset || stop || !run) begin
            bit_step <= 0;
            tail_step <= 0;
        end else if (step_end) begin
            // From the START cycle (no step) or TAIL: a first bit, unless
            // the last frame ended.
            bit_step <= tail_step ? !last_frame : !bit_step || !last_bit;
            tail_step <= bit_step && last_bit;
        end
        // STOP keeps the frames already finished. One clear condition, so
        // synthesis uses the flip-flop reset and nothing follows the adder.
        if (reset || (load && !stop)) completed <= 0;
        else if (finish && !stop) completed <= 16'd1 + completed;
    end

    // Step timing. A new step on the first tick ends after one tick if it
    // lasts two; counting down, the step ends when 1 or 2 ticks are left.
    always @(posedge clk) begin
        step_one <= step_one_next;
        step_two <= step_two_next;
        step_end <= step_one_next || step_two_next;
        ticks_three <= step_one ? next_four : step_two ? next_three : step_ticks == 5;
        ticks_four <= step_one ? next_five : step_two ? next_four : step_ticks == 6;
        // Ticks left, then N-1-e, N-2-e, N+L-1-e and N+L-2-e.
        step_ticks <= count(reload[0], late[0], {1'b0, step_ticks},
            {1'b0, next_plus_one});
        clock_count <= count(reload[1], late[1], clock_count, {3'b0, divider});
        clock_count_fall <= count(reload[2], late[2], clock_count_fall,
            {3'b0, divider_minus_one});
        latch_count <= count(reload[3], late[3], latch_count, {2'b0, latch_end});
        latch_count_fall <= count(reload[4], late[4], latch_count_fall,
            {2'b0, latch_end_minus_one});
        free_ticks <= count(free_reload, free_late, {3'b0, free_ticks},
            {2'b0, divider_plus_one});
        if (!run_for[1]) begin
            next_plus_one <= plan_bit_plus_one;
            next_two <= plan_divider_one;
            next_three <= 0;
            next_four <= plan_divider_two;
            next_five <= 0;
        end else if (end_for[1]) begin
            next_plus_one <= after_next_tail ? tail_plus_one : bit_plus_one;
            next_two <= after_next_tail ? tail_two : divider_one;
            next_three <= after_next_tail && tail_three;
            next_four <= after_next_tail ? tail_four : divider_two;
            next_five <= after_next_tail && tail_five;
        end
    end

    // Data path. While idle every register holds the start value of the
    // next SEND, taken from the plan; while running it follows the steps.
    always @(posedge clk) begin
        if (!run_for[0]) begin
            bits_left <= plan_bits;
            last_bit <= plan_bits_one;
            second_last_bit <= plan_bits_two;
            shift_word <= plan_word;
        end else if (bit_step && end_for[0]) begin
            if (last_bit) begin
                // TAIL follows: prepare the bits of the next frame.
                bits_left <= bit_count;
                last_bit <= bits_one;
                second_last_bit <= bits_two;
                shift_word <= word_config;
            end else begin
                bits_left <= bits_left - 1'b1;
                last_bit <= second_last_bit;
                second_last_bit <= bits_left == 3;
                shift_word <= {shift_word[24:0], 1'b0};
            end
        end
        // A continuous count wraps freely and never ends the emission.
        if (!run_for[2] || finish) begin
            remaining <= 16'hffff + (run_for[2] ? remaining : plan_repeat_plus_one[15:0]);
            last_frame <= run_for[2] ? !continuous && remaining == 2 : plan_single;
        end
        // One CLK edge every N ticks, two ticks per core cycle. An edge on
        // the first tick is reloaded with N-1; with N=1 both ticks toggle
        // and the level is unchanged.
        if (!run_for[2]) free_level <= 1;
        else if (free_one ? !divider_one : free_two) free_level <= !free_level;
        free_one <= free_one_next;
        free_two <= free_two_next;
        free_reload <= free_one_next || free_two_next;
        free_late <= free_two_next;
        free_three <= free_one ? divider_four : free_two ? divider_three : free_ticks == 5;
        free_four <= free_one ? divider_five : free_two ? divider_four : free_ticks == 6;
    end
endmodule
