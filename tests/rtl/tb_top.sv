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
    wire tr_pin;
    wire [3:0] led;
    reg [7:0] captured [0:255];
    integer clock_pin_edges=0, edges_at_status;
    always @(posedge clock_pin) clock_pin_edges=clock_pin_edges+1;
    integer captured_count=0;
    reg [111:0] payload;
    reg waveform_done=0;
    integer tick;
    integer core_edges=0;
    integer reset_checks=0;
    reg [2:0] expected;

    // A 2 ms LED hold (100 MHz control clock) keeps the pattern visible past
    // one 12-byte UART reply.
    arty_top #(.LED_HOLD_CYCLES(200000)) board(.clk100(clk100),.reset_n(reset_n),.uart_rx(host_serial),
        .uart_tx(board_serial),.data_out(data_pin),.frame_clk(clock_pin),
        .latch_enable(latch_pin),.tr_out(tr_pin),.led(led));
    always @(posedge board.core_clock or negedge board.core_clock)
        core_edges=core_edges+1;
    // The host side runs its own 200 MHz clock, unrelated to the board's.
    uart_tx #(.CLOCK_HZ(200000000)) host_tx(.clk(host_clock),.reset(!reset_n),.data(host_data),
        .valid(host_valid),.ready(host_ready),.tx(host_serial));
    uart_rx #(.CLOCK_HZ(200000000)) host_rx(.clk(host_clock),.reset(!reset_n),.rx(board_serial),
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

    // Stop the actual ODDR clock while selected outputs are high. A mask on
    // D1/D2 or CE alone cannot clear Q without another edge; the board's
    // asynchronous reset must still drive all three output pins low.
    task reset_without_clock;
        input [2:0] high_mask;
        integer frozen_edges;
        begin
            #0.1;
            if(({data_pin,clock_pin,latch_pin} & high_mask)!==high_mask)
                $fatal(1,"Reset test did not begin with the selected outputs high");
            if(board.core_clock===1'b1) force board.core_clock=1'b1;
            else force board.core_clock=1'b0;
            frozen_edges=core_edges;
            #1;
            reset_n=0;
            #0.1;
            if(core_edges!=frozen_edges || {data_pin,clock_pin,latch_pin}!==3'b000)
                $fatal(1,"Board reset did not clear ODDR pins with the clock stopped");
            #20;
            if(core_edges!=frozen_edges || {data_pin,clock_pin,latch_pin}!==3'b000)
                $fatal(1,"Reset pins changed or a core clock edge occurred while frozen");
            reset_checks=reset_checks+1;
            release board.core_clock;
            @(negedge clk100); reset_n=1;
            wait(led[0]);
            wait(!board.reset);
            repeat(10) @(posedge host_clock);
            captured_count=0;
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

    // Same as check_response, but the 16-bit frame counter is whatever the
    // board reports: STOP leaves the counter of a continuous emission as it is.
    task check_response_any_count;
        input integer offset;
        input [7:0] op,seq,status,busy;
        begin
            wait(captured_count>=offset+12);
            #0.1;
            check_response(offset,op,seq,status,busy,{captured[offset+9],captured[offset+8]});
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

        // LED test through the production UART: the pattern replaces the
        // status LEDs, expires back to {completed, 0, busy, locked}, and an
        // automatic-mode command restores the status display at once.
        payload=0; payload[7:0]=8'h86;
        request(5,38,1);
        check_response(36,5,38,0,0,1);
        if(led!==4'b0110) $fatal(1,"LED pattern not shown: %b",led);
        wait(led===4'b1001);
        request(5,39,1);
        check_response(48,5,39,0,0,1);
        if(led!==4'b0110) $fatal(1,"Second LED pattern not shown: %b",led);
        payload[7:0]=8'h00;
        request(5,40,1);
        check_response(60,5,40,0,0,1);
        if(led!==4'b1001) $fatal(1,"Automatic LED mode did not restore status: %b",led);
        // INFO page 1: low half of the 200 MHz core clock frequency.
        payload[7:0]=1;
        request(6,41,1);
        check_response(72,6,41,0,0,16'hc200);
        // INFO page 3: capabilities LED, INFO, continuous SEND, free CLK, TR.
        payload[7:0]=3;
        request(6,42,1);
        check_response(84,6,42,0,0,16'h001f);

        // Continuous SEND (repeat_count 0), 10 ns frames: during the
        // milliseconds of UART traffic the frame counter wraps several
        // times, CLK keeps running, and only STOP returns the pins to idle.
        payload=0;
        payload[31:0]=1; payload[39:32]=1; payload[55:40]=1;
        payload[71:56]=1; payload[103:88]=0;
        request(2,43,14);
        check_response(96,2,43,0,1,0);
        request(4,44,0);
        wait(captured_count>=120);
        #0.1;
        if(captured[111]!==8'h84 || captured[114]!==0 || captured[115]!==1)
            $fatal(1,"Continuous emission not reported busy by STATUS");
        edges_at_status=clock_pin_edges;
        if(edges_at_status<100000) $fatal(1,"CLK did not run continuously: %0d edges",
            edges_at_status);
        repeat(1000) @(posedge clk100);
        if(clock_pin_edges<=edges_at_status) $fatal(1,"CLK halted before STOP");
        request(3,45,0);
        wait(captured_count>=132);
        #0.1;
        if(captured[123]!==8'h83 || captured[126]!==0 || captured[127]!==0)
            $fatal(1,"STOP of continuous emission not acknowledged");
        #20;
        edges_at_status=clock_pin_edges;
        repeat(100) @(posedge clk100);
        if({data_pin,clock_pin,latch_pin}!==3'b000 || clock_pin_edges!=edges_at_status)
            $fatal(1,"Pins still active after STOP of continuous emission");

        // Free CLK, continuous: 1 bit, N=1, LATCH one CLK period, no pause.
        // CLK must rise once per 5 ns core cycle, LATCH and pause included.
        payload=0;
        payload[31:0]=1; payload[39:32]=1; payload[55:40]=1;
        payload[71:56]=1; payload[103:88]=0; payload[111:104]=4;
        request(2,46,14);
        check_response(132,2,46,0,1,0);
        // Exactly 10 us: from one clk100 edge to the 1000th following edge.
        @(posedge clk100);
        edges_at_status=clock_pin_edges;
        repeat(1000) @(posedge clk100);
        if(clock_pin_edges-edges_at_status<1999 || clock_pin_edges-edges_at_status>2001)
            $fatal(1,"Free CLK not periodic: %0d rising edges in 2000 core cycles",
                clock_pin_edges-edges_at_status);
        request(3,47,0);
        wait(captured_count>=156);
        #0.1;
        if(captured[147]!==8'h83 || captured[150]!==0 || captured[151]!==0)
            $fatal(1,"STOP of free-CLK emission not acknowledged");
        #20;
        edges_at_status=clock_pin_edges;
        repeat(100) @(posedge clk100);
        if({data_pin,clock_pin,latch_pin}!==3'b000 || clock_pin_edges!=edges_at_status)
            $fatal(1,"Pins still active after STOP of free-CLK emission");

        // TR: a static level set through the production UART. It starts low,
        // follows valid commands, ignores an invalid one, and the board reset
        // below returns it to 0 V.
        if(tr_pin!==1'b0) $fatal(1,"TR not low at start");
        payload=0; payload[7:0]=8'h01;
        request(7,48,1);
        check_response_any_count(156,7,48,0,0);
        if(tr_pin!==1'b1) $fatal(1,"TR did not go high");
        payload[7:0]=8'h02;
        request(7,49,1);
        check_response_any_count(168,7,49,2,0);
        if(tr_pin!==1'b1) $fatal(1,"Invalid TR command changed the pin");
        payload[7:0]=8'h00;
        request(7,50,1);
        check_response_any_count(180,7,50,0,0);
        if(tr_pin!==1'b0) $fatal(1,"TR did not return low");
        payload[7:0]=8'h01;
        request(7,51,1);
        check_response_any_count(192,7,51,0,0);
        if(tr_pin!==1'b1) $fatal(1,"TR did not go high again");

        // Repeated long frames leave enough time to interrupt DATA/CLK and
        // LATCH independently. Commands still arrive over the production UART.
        payload=0;
        payload[31:0]=1; payload[39:32]=1; payload[55:40]=100;
        payload[71:56]=100; payload[103:88]=65535;
        request(2,34,14);
        wait(data_pin && clock_pin);
        reset_without_clock(3'b110);
        if(tr_pin!==1'b0) $fatal(1,"Reset did not return TR to 0 V");
        request(1,35,0);
        check_response(0,1,35,0,0,0);
        request(2,36,14);
        wait(latch_pin);
        reset_without_clock(3'b001);
        request(1,37,0);
        check_response(0,1,37,0,0,0);
        if(reset_checks!=2) $fatal(1,"Missing asynchronous reset cases");
        $display("PASS tb_top: UART PING/SEND/STATUS/LED/INFO/TR, 200 MHz modeled burst, continuous and free-CLK SEND/STOP, %0d stopped-clock asynchronous resets and UART recovery",reset_checks);
        $finish;
    end
    initial begin #60000000; $fatal(1,"Timeout"); end
endmodule
