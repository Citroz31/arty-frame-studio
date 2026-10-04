`timescale 1ns/1ps
module tb_engine;
    reg clk=0;
    always #2.5 clk=~clk;
    reg reset=1, start=0, stop=0;
    reg [31:0] word_in;
    reg [4:0] bits_in;
    reg [15:0] divider_in, latch_ticks_in, gap_ticks_in, repeat_in;
    reg [1:0] flags_in;
    wire busy;
    wire [15:0] completed;
    wire dr, df, cr, cf, lr, lf;
    integer checks=0;
    frame_engine dut (
        .clk(clk), .reset(reset), .start(start), .stop(stop),
        .word_in(word_in), .bits_in(bits_in), .divider_in(divider_in),
        .latch_ticks_in(latch_ticks_in), .gap_ticks_in(gap_ticks_in),
        .repeat_in(repeat_in), .flags_in(flags_in), .busy(busy),
        .completed(completed), .data_rise(dr), .data_fall(df),
        .clock_rise(cr), .clock_fall(cf), .latch_rise(lr), .latch_fall(lf)
    );

    task check_tick;
        input integer tick;
        input integer duration;
        input observed_data, observed_clock, observed_latch;
        integer offset, bit_number;
        reg expected_data, expected_clock, expected_latch;
        begin
            expected_data=0;
            expected_clock=0;
            expected_latch=flags_in[1];
            // repeat_in=0: continuous, every tick lies inside a frame.
            if (repeat_in == 0 || tick < duration*repeat_in) begin
                offset=tick%duration;
                if (offset < 2*divider_in*bits_in) begin
                    bit_number=offset/(2*divider_in);
                    expected_data=flags_in[0] ? word_in[bit_number]
                        : word_in[bits_in-1-bit_number];
                    expected_clock=(offset%(2*divider_in)) >= divider_in;
                end
                if (offset >= 2*divider_in*bits_in+divider_in
                    && offset < 2*divider_in*bits_in+divider_in+latch_ticks_in)
                    expected_latch=!flags_in[1];
            end
            if ({observed_data,observed_clock,observed_latch}
                !== {expected_data,expected_clock,expected_latch})
                $fatal(1,"Tick %0d: actual %b%b%b expected %b%b%b (N=%0d bits=%0d flags=%0d)",
                    tick,observed_data,observed_clock,observed_latch,
                    expected_data,expected_clock,expected_latch,
                    divider_in,bits_in,flags_in);
            checks=checks+1;
        end
    endtask

    task run_case;
        input [31:0] word_value;
        input integer bit_value, divider_value, latch_value, gap_value, repeat_value;
        input [1:0] flags_value;
        integer duration, pair_index, pairs;
        begin
            @(negedge clk);
            word_in=word_value; bits_in=bit_value; divider_in=divider_value;
            latch_ticks_in=latch_value; gap_ticks_in=gap_value;
            repeat_in=repeat_value; flags_in=flags_value; start=1;
            @(posedge clk); #0.1;
            if (!busy || completed != 0) $fatal(1,"SEND did not start/reset counter");
            @(negedge clk); start=0;
            duration=2*divider_value*bit_value+divider_value+latch_value+gap_value;
            pairs=(duration*repeat_value+5)/2;
            for (pair_index=0; pair_index<pairs; pair_index=pair_index+1) begin
                @(posedge clk);
                check_tick(pair_index*2,duration,dr,cr,lr);
                check_tick(pair_index*2+1,duration,df,cf,lf);
            end
            #0.1;
            if (busy || completed != repeat_value)
                $fatal(1,"Completion: busy=%b completed=%0d expected=%0d",busy,completed,repeat_value);
        end
    endtask

    // Continuous SEND (repeat 0): the frame repeats past every 16-bit
    // boundary of the hidden repetition counter until STOP, at any phase.
    task run_continuous_case;
        input [31:0] word_value;
        input integer bit_value, divider_value, latch_value, gap_value;
        input integer frames, stop_offset;
        input [1:0] flags_value;
        integer duration, pair_index, pairs;
        begin
            @(negedge clk);
            word_in=word_value; bits_in=bit_value; divider_in=divider_value;
            latch_ticks_in=latch_value; gap_ticks_in=gap_value;
            repeat_in=0; flags_in=flags_value; start=1;
            @(posedge clk); #0.1;
            if (!busy || completed != 0) $fatal(1,"Continuous SEND did not start");
            @(negedge clk); start=0;
            duration=2*divider_value*bit_value+divider_value+latch_value+gap_value;
            pairs=(duration*frames+stop_offset)/2;
            for (pair_index=0; pair_index<pairs; pair_index=pair_index+1) begin
                @(posedge clk);
                check_tick(pair_index*2,duration,dr,cr,lr);
                check_tick(pair_index*2+1,duration,df,cf,lf);
            end
            #0.1;
            if (!busy || completed != ((pairs*2)/duration)%65536)
                $fatal(1,"Continuous: busy=%b completed=%0d after %0d ticks (frame %0d)",
                    busy,completed,pairs*2,duration);
            @(negedge clk); stop=1;
            @(posedge clk); #0.1;
            if (busy || dr || df || cr || cf || lr !== flags_value[1] || lf !== flags_value[1])
                $fatal(1,"STOP did not end continuous emission");
            @(negedge clk); stop=0;
            repeat (3) begin
                @(posedge clk); #0.1;
                if (busy || dr || df || cr || cf || lr !== flags_value[1])
                    $fatal(1,"Outputs moved after STOP of continuous emission");
            end
            repeat_in=1;
        end
    endtask

    integer width_case, flag_case;

    initial begin
        word_in=0; bits_in=1; divider_in=1; latch_ticks_in=1;
        gap_ticks_in=0; repeat_in=1; flags_in=0;
        repeat (4) @(posedge clk);
        @(negedge clk); reset=0;
        run_case(32'h2a,6,1,1,0,1,0);
        run_case(32'h15,6,1,3,0,3,3);
        run_case(32'h123456,26,3,5,7,2,0);
        run_case(32'h123456,26,2,4,8,2,1);
        run_case(32'h3ffffff,26,1,1,0,1,0);
        run_case(1,1,7,1,0,2,2);
        run_case(0,1,65535,1,0,1,0);
        run_case(0,1,1,1,0,65535,0);
        // Random legal short cases exercise parity and state transitions in
        // either half of a core clock; seed is deterministic under Icarus.
        repeat (60) begin
            run_case($urandom_range(0,63),6,$urandom_range(1,7),
                $urandom_range(1,9),$urandom_range(0,9),
                $urandom_range(1,3),$urandom_range(0,3));
        end
        // Exercise the SEND-time alignment for every supported frame width.
        for (width_case=1; width_case<=26; width_case=width_case+1) begin
            repeat (20) begin
                run_case($urandom_range(0,(1<<width_case)-1),width_case,
                    $urandom_range(1,7),$urandom_range(1,9),
                    $urandom_range(0,9),$urandom_range(1,3),$urandom_range(0,3));
            end
        end
        for (flag_case=0; flag_case<4; flag_case=flag_case+1)
            run_case(32'h123456,26,1,65535,65535,1,flag_case);
        // Shortest frame (4 ticks): 70000 frames cross the 16-bit wrap twice
        // for the hidden counter and once for completed, without ending.
        run_continuous_case(1,1,1,1,0,70000,1,0);
        run_continuous_case(32'h2a,6,1,3,0,40,3,3);
        run_continuous_case(32'h123456,26,3,5,7,5,17,1);
        repeat (40) begin
            run_continuous_case($urandom_range(0,63),6,$urandom_range(1,7),
                $urandom_range(1,9),$urandom_range(0,9),$urandom_range(2,4),
                $urandom_range(0,40),$urandom_range(0,3));
        end
        // STOP must preserve completed, including repetitions already finished.
        @(negedge clk);
        word_in=1; bits_in=1; divider_in=10; latch_ticks_in=1;
        gap_ticks_in=1; repeat_in=100; flags_in=2; start=1;
        @(posedge clk); #0.1;
        @(negedge clk); start=0;
        wait(completed >= 1);
        @(negedge clk); stop=1;
        @(posedge clk); #0.1;
        if (busy || dr || df || cr || cf || !lr || !lf || completed == 0)
            $fatal(1,"STOP did not preserve completed/idle polarity");
        @(negedge clk); stop=0;
        $display("PASS tb_engine: %0d half-tick waveform checks",checks);
        $finish;
    end
    initial begin #100000000; $fatal(1,"Timeout"); end
endmodule
