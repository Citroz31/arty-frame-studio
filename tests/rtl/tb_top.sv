`timescale 1ns/1ps
// Integration: real 115200 UART -> packet parser -> engine -> SAME_EDGE ODDR.
// PLL/ODDR models verify functional wiring only, not physical timing closure.
module tb_top;
    reg clk100=0, host_clock=0, reset_n=0;
    always #5 clk100=~clk100;
    always #2.5 host_clock=~host_clock;
    reg [7:0] host_data=0;
    reg host_valid=0;
    wire host_ready, host_serial, board_serial;
    wire [7:0] returned_data;
    wire returned_valid;
    wire data_pin, clock_pin, latch_pin;
    wire [3:0] led;
    reg [7:0] captured [0:127];
    integer captured_count=0;
    reg [111:0] payload;
    reg waveform_done=0;
    integer tick;
    reg [2:0] expected;

    arty_top board(.clk100(clk100),.reset_n(reset_n),.uart_rx(host_serial),
        .uart_tx(board_serial),.data_out(data_pin),.frame_clk(clock_pin),
        .latch_enable(latch_pin),.led(led));
    uart_tx host_tx(.clk(host_clock),.reset(!reset_n),.data(host_data),
        .valid(host_valid),.ready(host_ready),.tx(host_serial));
    uart_rx host_rx(.clk(host_clock),.reset(!reset_n),.rx(board_serial),
        .data(returned_data),.valid(returned_valid));
    always @(posedge host_clock) if(returned_valid) begin
        captured[captured_count]=returned_data;
        captured_count=captured_count+1;
    end

    function [15:0] crc_byte;
        input [15:0] previous;
        input [7:0] value;
        integer index;
        reg [15:0] current;
        begin
            current=previous^{value,8'b0};
            for(index=0;index<8;index=index+1)
                current=current[15] ? (current<<1)^16'h1021 : current<<1;
            crc_byte=current;
        end
    endfunction
    task send_byte;
        input [7:0] value;
        begin
            wait(host_ready);
            @(negedge host_clock); host_data=value; host_valid=1;
            @(negedge host_clock); host_valid=0;
        end
    endtask
    task request;
        input [7:0] op, seq;
        input integer length;
        reg [15:0] crc;
        integer index;
        begin
            send_byte(8'ha7); send_byte(8'h7a);
            crc=crc_byte(16'hffff,1); send_byte(1);
            crc=crc_byte(crc,op); send_byte(op);
            crc=crc_byte(crc,seq); send_byte(seq);
            crc=crc_byte(crc,length); send_byte(length);
            for(index=0;index<length;index=index+1) begin
                crc=crc_byte(crc,payload[index*8+:8]);
                send_byte(payload[index*8+:8]);
            end
            send_byte(crc[7:0]); send_byte(crc[15:8]);
        end
    endtask
    task check_response;
        input integer offset;
        input [7:0] op,seq,status,busy;
        input [15:0] completed;
        integer index;
        reg [15:0] crc;
        begin
            wait(captured_count>=offset+12);
            #0.1;
            if(captured[offset]!==8'ha7 || captured[offset+1]!==8'h7a
                || captured[offset+2]!==1 || captured[offset+3]!==(op|8'h80)
                || captured[offset+4]!==seq || captured[offset+5]!==4
                || captured[offset+6]!==status || captured[offset+7]!==busy
                || {captured[offset+9],captured[offset+8]}!==completed)
                $fatal(1,"Top UART response mismatch");
            crc=16'hffff;
            for(index=2;index<10;index=index+1) crc=crc_byte(crc,captured[offset+index]);
            if({captured[offset+11],captured[offset+10]}!==crc)
                $fatal(1,"Top UART response CRC mismatch");
        end
    endtask

    // Golden pin waveform for word 10b, N=1, latch_ticks=2, gap_ticks=1.
    initial begin
        @(posedge data_pin);
        for(tick=0;tick<10;tick=tick+1) begin
            case(tick)
                0: expected=3'b100;
                1: expected=3'b110;
                2: expected=3'b000;
                3: expected=3'b010;
                5,6: expected=3'b001;
                default: expected=3'b000;
            endcase
            #0.1;
            if({data_pin,clock_pin,latch_pin}!==expected)
                $fatal(1,"Top pin waveform tick%0d actual%b%b%b expected%b",tick,
                    data_pin,clock_pin,latch_pin,expected);
            #2.4;
        end
        waveform_done=1;
    end
    initial begin
        payload=0;
        repeat(8) @(posedge clk100);
        @(negedge clk100); reset_n=1;
        wait(led[0]); repeat(10) @(posedge host_clock);
        request(1,31,0);
        check_response(0,1,31,0,0,0);
        payload[31:0]=2; payload[39:32]=2; payload[55:40]=1;
        payload[71:56]=2; payload[87:72]=1; payload[103:88]=1;
        request(2,32,14);
        check_response(12,2,32,0,1,0);
        if(!waveform_done) $fatal(1,"Top SEND emitted no waveform");
        request(4,33,0);
        check_response(24,4,33,0,0,1);
        $display("PASS tb_top: production UART PING/SEND/STATUS and 200 MHz modeled pin burst");
        $finish;
    end
    initial begin #10000000; $fatal(1,"Timeout"); end
endmodule
