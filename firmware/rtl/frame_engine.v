`timescale 1ns/1ps
// Two logical 2.5 ns steps per 200 MHz cycle. Connect *_rise/*_fall to
// ODDR D1/D2 with DDR_CLK_EDGE="SAME_EDGE". No 400 MHz fabric clock.
module frame_engine (
    input wire clk, input wire reset,
    input wire start, input wire stop,
    input wire [31:0] word_in,
    input wire [4:0] bits_in,
    input wire [15:0] divider_in, latch_ticks_in, gap_ticks_in, repeat_in,
    input wire [1:0] flags_in,
    output wire busy,
    output reg [15:0] completed,
    output reg data_rise, data_fall,
    output reg clock_rise, clock_fall,
    output reg latch_rise, latch_fall
);
    localparam IDLE=0, RISE_WAIT=1, FALL_WAIT=2, PRE_LATCH=3,
               LATCH_HOLD=4, GAP_WAIT=5;
    reg [2:0] phase;
    reg [31:0] word_config;
    reg [4:0] bit_count, bit_index;
    reg [15:0] divider, latch_ticks, gap_ticks, remaining, ticks;
    reg [1:0] flags;
    reg data_level, clock_level, latch_level;

    reg [2:0] next_phase;
    reg [4:0] next_bit_index;
    reg [15:0] next_remaining, next_ticks, next_completed;
    reg next_data, next_clock, next_latch;

    assign busy = phase != IDLE;

    function selected_bit;
        input [4:0] index;
        begin
            selected_bit = flags[0] ? word_config[index]
                                   : word_config[bit_count - 1'b1 - index];
        end
    endfunction

    task finish_frame;
        begin
            next_completed = next_completed + 1'b1;
            if (next_remaining == 1) begin
                next_remaining = 0;
                next_phase = IDLE;
                next_data = 0;
                next_clock = 0;
            end else begin
                next_remaining = next_remaining - 1'b1;
                next_bit_index = 0;
                next_data = selected_bit(0);
                next_phase = RISE_WAIT;
                next_ticks = divider;
            end
        end
    endtask

    task half_step;
        begin
            if (next_phase != IDLE) begin
                if (next_ticks > 1) begin
                    next_ticks = next_ticks - 1'b1;
                end else begin
                    case (next_phase)
                        RISE_WAIT: begin
                            next_clock = 1;
                            next_phase = FALL_WAIT;
                            next_ticks = divider;
                        end
                        FALL_WAIT: begin
                            next_clock = 0;
                            next_ticks = divider;
                            if (next_bit_index + 1'b1 == bit_count) begin
                                next_data = 0;
                                next_phase = PRE_LATCH;
                            end else begin
                                next_bit_index = next_bit_index + 1'b1;
                                next_data = selected_bit(next_bit_index);
                                next_phase = RISE_WAIT;
                            end
                        end
                        PRE_LATCH: begin
                            next_latch = !flags[1];
                            next_phase = LATCH_HOLD;
                            next_ticks = latch_ticks;
                        end
                        LATCH_HOLD: begin
                            next_latch = flags[1];
                            if (gap_ticks == 0)
                                finish_frame;
                            else begin
                                next_phase = GAP_WAIT;
                                next_ticks = gap_ticks;
                            end
                        end
                        GAP_WAIT: finish_frame;
                        default: next_phase = IDLE;
                    endcase
                end
            end
        end
    endtask

    always @* begin
        next_phase = phase;
        next_bit_index = bit_index;
        next_remaining = remaining;
        next_ticks = ticks;
        next_completed = completed;
        next_data = data_level;
        next_clock = clock_level;
        next_latch = latch_level;
        data_rise = next_data;
        clock_rise = next_clock;
        latch_rise = next_latch;
        half_step;
        data_fall = next_data;
        clock_fall = next_clock;
        latch_fall = next_latch;
        half_step;
    end

    always @(posedge clk) begin
        if (reset) begin
            phase <= IDLE;
            word_config <= 0;
            bit_count <= 1;
            bit_index <= 0;
            divider <= 1;
            latch_ticks <= 1;
            gap_ticks <= 0;
            remaining <= 0;
            ticks <= 0;
            flags <= 0;
            completed <= 0;
            data_level <= 0;
            clock_level <= 0;
            latch_level <= 0;
        end else if (stop) begin
            phase <= IDLE;
            remaining <= 0;
            data_level <= 0;
            clock_level <= 0;
            latch_level <= flags[1];
        end else if (start && !busy) begin
            word_config <= word_in;
            bit_count <= bits_in;
            bit_index <= 0;
            divider <= divider_in;
            latch_ticks <= latch_ticks_in;
            gap_ticks <= gap_ticks_in;
            remaining <= repeat_in;
            ticks <= divider_in;
            flags <= flags_in;
            completed <= 0;
            phase <= RISE_WAIT;
            data_level <= flags_in[0] ? word_in[0] : word_in[bits_in-1'b1];
            clock_level <= 0;
            latch_level <= flags_in[1];
        end else begin
            phase <= next_phase;
            bit_index <= next_bit_index;
            remaining <= next_remaining;
            ticks <= next_ticks;
            completed <= next_completed;
            data_level <= next_data;
            clock_level <= next_clock;
            latch_level <= next_latch;
        end
    end
endmodule
