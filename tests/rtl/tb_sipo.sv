`timescale 1ns/1ps
// Independent 8-bit shift/storage-register receiver at 10 MHz. Capture
// happens on STCP's rising edge, as on a 74HC595; it is not a transparent
// latch and is not an SPI chip select.
module tb_sipo;
    reg clk=0;
    always #2.5 clk=~clk;
    reg reset=1, start=0, stop=0;
    reg [15:0] latch_ticks=40, gap_ticks=40, repeat_count=3;
    reg [2:0] flags=0;
    wire busy;
    wire [15:0] completed;
    wire dr, df, cr, cf, lr, lf;
    wire data_pin, clock_pin, latch_pin;
    frame_engine engine(
        .clk(clk), .reset(reset), .start(start), .stop(stop),
        .word_in(32'ha5), .bits_in(5'd8), .divider_in(16'd20),
        .latch_ticks_in(latch_ticks), .gap_ticks_in(gap_ticks),
        .repeat_in(repeat_count), .flags_in(flags), .busy(busy),
        .completed(completed), .data_rise(dr), .data_fall(df),
        .clock_rise(cr), .clock_fall(cf), .latch_rise(lr), .latch_fall(lf)
    );
    ODDR data_ddr(
        .C(clk), .CE(1'b1), .D1(dr), .D2(df), .R(reset), .S(1'b0), .Q(data_pin)
    );
    ODDR clock_ddr(
        .C(clk), .CE(1'b1), .D1(cr), .D2(cf), .R(reset), .S(1'b0), .Q(clock_pin)
    );
    ODDR latch_ddr(
        .C(clk), .CE(1'b1), .D1(lr), .D2(lf), .R(reset), .S(1'b0), .Q(latch_pin)
    );

    reg [7:0] shift_register=0, output_register=0;
    reg [7:0] expected_capture=8'ha5;
    reg observe=0;
    integer rising_edges=0, captures=0, total_captures=0, cases=0;

    always @(posedge clock_pin) begin
        shift_register <= {shift_register[6:0], data_pin};
        if (observe) rising_edges=rising_edges+1;
    end
    always @(posedge latch_pin) begin
        output_register <= shift_register;
        // With active-low LATCH, SEND first establishes its high idle level.
        // That edge precedes the transfer and is not a completed word.
        if (observe && rising_edges != 0) begin
            if (shift_register !== expected_capture)
                $fatal(1,"SIPO capture %0d: received %02h expected %02h (flags=%0d)",
                    captures,shift_register,expected_capture,flags);
            captures=captures+1;
            total_captures=total_captures+1;
        end
    end

    task run_case;
        input [2:0] mode;
        input integer repeat_value;
        input [7:0] expected_value;
        integer expected_frames, expected_edges;
        begin
            @(negedge clk); reset=1; observe=0;
            repeat(3) @(posedge clk);
            @(negedge clk);
            shift_register=0; output_register=0;
            rising_edges=0; captures=0;
            flags=mode;
            latch_ticks=mode[2] ? 20 : 40;
            gap_ticks=40;
            repeat_count=repeat_value;
            expected_capture=expected_value;
            reset=0; observe=1; start=1;
            @(negedge clk); start=0;
            if (repeat_value == 0) begin
                wait(completed==5);
                @(negedge clk); stop=1;
                @(negedge clk); stop=0;
                expected_frames=5;
            end else begin
                wait(!busy);
                expected_frames=repeat_value;
            end
            // Drain the ODDR pipeline before assessing physical pin activity.
            repeat(4) @(posedge clk);
            #0.1;
            expected_edges=expected_frames*(mode[2] ? 10 : 8);
            if (busy || completed!=expected_frames || captures!=expected_frames
                || rising_edges!=expected_edges || output_register!==expected_value)
                $fatal(1,"SIPO final: busy=%b completed=%0d captures=%0d clocks=%0d output=%02h",
                    busy,completed,captures,rising_edges,output_register);
            if (clock_pin || data_pin || latch_pin!==mode[1])
                $fatal(1,"SIPO pins not idle after completion/STOP");
            observe=0;
            cases=cases+1;
        end
    endtask

    initial begin
        // Gated CLK delivers exactly 8 rising edges per A5 capture.
        run_case(0,3,8'ha5);
        // Free CLK delivers 8 data + 1 LATCH + 1 gap edge. Active-high STCP
        // captures before the two extra zero shifts, preserving the output.
        run_case(4,3,8'ha5);
        // An active-low LATCH connected to rising-edge STCP captures on its
        // release, after one zero shift: A5 becomes 4A. This is a deliberate
        // compatibility counterexample, not a promise to preserve A5.
        run_case(6,3,8'h4a);
        // Infinite free CLK still latches each complete word correctly and
        // STOP preserves the already captured output while pins become idle.
        run_case(4,0,8'ha5);
        $display("PASS tb_sipo: %0d modes, %0d receiver captures at 10 MHz, active-low release counterexample and continuous STOP",cases,total_captures);
        $finish;
    end
    initial begin #100000; $fatal(1,"Timeout"); end
endmodule
