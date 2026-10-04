`timescale 1ns/1ps
module tb_oddr;
    reg clk=0;
    always #2.5 clk=~clk;
    reg reset=1,start=0;
    wire dr,df,cr,cf,lr,lf,busy;
    wire [15:0] completed;
    wire data_pin,clock_pin,latch_pin;
    reg expected_data,expected_clock,expected_latch;
    reg saved_data,saved_clock,saved_latch;
    integer checks=0;
    frame_engine engine(.clk(clk),.reset(reset),.start(start),.stop(1'b0),
        .word_in(32'h2a),.bits_in(5'd6),.divider_in(16'd1),
        .latch_ticks_in(16'd1),.gap_ticks_in(16'd0),.repeat_in(16'd2),
        .flags_in(3'd0),.busy(busy),.completed(completed),
        .data_rise(dr),.data_fall(df),.clock_rise(cr),.clock_fall(cf),
        .latch_rise(lr),.latch_fall(lf));
    ODDR data_ddr(.C(clk),.CE(1'b1),.D1(dr),.D2(df),.R(reset),.S(1'b0),.Q(data_pin));
    ODDR clock_ddr(.C(clk),.CE(1'b1),.D1(cr),.D2(cf),.R(reset),.S(1'b0),.Q(clock_pin));
    ODDR latch_ddr(.C(clk),.CE(1'b1),.D1(lr),.D2(lf),.R(reset),.S(1'b0),.Q(latch_pin));
    always @(posedge clk) begin
        expected_data=dr; expected_clock=cr; expected_latch=lr;
        saved_data=df; saved_clock=cf; saved_latch=lf;
        #0.1;
        if(!reset && {data_pin,clock_pin,latch_pin}!=={expected_data,expected_clock,expected_latch})
            $fatal(1,"ODDR rising-edge capture mismatch");
        checks=checks+1;
    end
    always @(negedge clk) begin
        #0.1;
        if(!reset && {data_pin,clock_pin,latch_pin}!=={saved_data,saved_clock,saved_latch})
            $fatal(1,"SAME_EDGE ODDR did not hold rising-edge D2 for falling edge");
        checks=checks+1;
    end
    initial begin
        repeat(4) @(posedge clk);
        @(negedge clk); reset=0; start=1;
        @(negedge clk); start=0;
        wait(completed==2);
        repeat(3) @(posedge clk);
        $display("PASS tb_oddr: %0d aligned pin-edge checks",checks);
        $finish;
    end
    initial begin #10000; $fatal(1,"Timeout"); end
endmodule
