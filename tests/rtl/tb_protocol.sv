`timescale 1ns/1ps
module tb_protocol;
    reg clk=0;
    always #2.5 clk=~clk;
    reg reset=1;
    reg [7:0] rx_data=0;
    reg rx_valid=0;
    wire [7:0] tx_data;
    wire tx_valid;
    reg tx_ready=1;
    wire busy;
    wire [15:0] completed;
    wire dr,df,cr,cf,lr,lf;
    reg [7:0] captured [0:1023];
    integer captured_count=0;
    reg [255:0] test_payload=0;
    integer requests=0;
    wire led_write;
    wire [4:0] led_value;
    reg [4:0] last_led=0;
    integer led_writes=0;
    integer page;
    reg [15:0] info_expected [0:5];
    integer stopped_completed;
    frame_controller #(
        .PACKET_TIMEOUT_CYCLES(60), .CORE_HZ(32'd150000000), .BUILD_ID(32'hA5C31E2D)
    ) dut (
        .clk(clk), .reset(reset), .rx_data(rx_data), .rx_valid(rx_valid),
        .tx_data(tx_data), .tx_valid(tx_valid), .tx_ready(tx_ready),
        .busy(busy), .completed(completed), .data_rise(dr), .data_fall(df),
        .clock_rise(cr), .clock_fall(cf), .latch_rise(lr), .latch_fall(lf),
        .led_write(led_write), .led_value(led_value)
    );
    always @(posedge clk) if (led_write) begin
        last_led=led_value;
        led_writes=led_writes+1;
    end
    always @(posedge clk) if (tx_valid && tx_ready) begin
        captured[captured_count]=tx_data;
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
            @(negedge clk); rx_data=value; rx_valid=1;
            @(negedge clk); rx_valid=0;
        end
    endtask

    task request;
        input [7:0] version_value,op_value,seq_value;
        input integer length_value;
        input corrupt;
        integer index;
        reg [7:0] value;
        reg [15:0] crc;
        begin
            crc=16'hffff;
            send_byte(8'ha7); send_byte(8'h7a);
            send_byte(version_value); crc=crc_byte(crc,version_value);
            send_byte(op_value); crc=crc_byte(crc,op_value);
            send_byte(seq_value); crc=crc_byte(crc,seq_value);
            send_byte(length_value); crc=crc_byte(crc,length_value);
            for(index=0;index<length_value;index=index+1) begin
                value=index<32 ? test_payload[index*8+:8] : 8'h5a;
                send_byte(value); crc=crc_byte(crc,value);
            end
            send_byte(crc[7:0] ^ (corrupt ? 8'h01 : 8'h00));
            send_byte(crc[15:8]);
        end
    endtask

    task check_reply;
        input integer start_index;
        input [7:0] op_value,seq_value,status_value;
        input integer busy_value,completed_value;
        integer index;
        reg [15:0] crc;
        begin
            wait(captured_count >= start_index+12);
            #0.1;
            if(captured[start_index]!==8'ha7 || captured[start_index+1]!==8'h7a
                || captured[start_index+2]!==1 || captured[start_index+3]!==(op_value|8'h80)
                || captured[start_index+4]!==seq_value || captured[start_index+5]!==4
                || captured[start_index+6]!==status_value)
                $fatal(1,"Reply header/status incorrect at %0d (status %0d wanted %0d)",
                    start_index,captured[start_index+6],status_value);
            if(busy_value>=0 && captured[start_index+7]!==busy_value)
                $fatal(1,"Reply busy incorrect");
            if(completed_value>=0 && {captured[start_index+9],captured[start_index+8]}!==completed_value)
                $fatal(1,"Reply completed incorrect");
            crc=16'hffff;
            for(index=2;index<10;index=index+1)
                crc=crc_byte(crc,captured[start_index+index]);
            if({captured[start_index+11],captured[start_index+10]}!==crc)
                $fatal(1,"Response CRC incorrect");
            requests=requests+1;
        end
    endtask

    task transaction;
        input [7:0] version_value,op_value,seq_value;
        input integer length_value;
        input corrupt;
        input [7:0] status_value;
        input integer busy_value,completed_value;
        integer start_index;
        begin
            start_index=captured_count;
            request(version_value,op_value,seq_value,length_value,corrupt);
            check_reply(start_index,op_value,seq_value,status_value,busy_value,completed_value);
        end
    endtask

    initial begin
        repeat(4) @(posedge clk);
        @(negedge clk); reset=0;
        transaction(1,1,7,0,0,0,0,0);
        transaction(1,1,8,0,1,4,0,0);
        transaction(2,1,9,0,0,5,0,0);
        transaction(1,99,10,0,0,1,0,0);
        transaction(1,1,11,1,0,2,0,0);
        transaction(1,2,12,13,0,2,0,0);
        transaction(1,2,13,14,0,2,0,0); // all-zero SEND invalid
        test_payload=0;
        test_payload[31:0]=32'h12345;
        test_payload[39:32]=26;
        test_payload[55:40]=100;
        test_payload[71:56]=4;
        test_payload[87:72]=3;
        test_payload[103:88]=20;
        test_payload[111:104]=2;
        transaction(1,2,14,14,0,0,1,0);
        transaction(1,2,15,14,0,3,1,0);
        transaction(1,4,16,0,0,0,1,0);
        transaction(1,1,17,0,0,0,1,0);
        transaction(1,3,18,0,0,0,0,0);
        if(busy || dr || df || cr || cf || !lr || !lf)
            $fatal(1,"STOP not reflected by engine");
        // Oversize drain must resynchronize, and an incomplete packet must time out.
        transaction(1,2,19,33,0,2,0,0);
        send_byte(8'ha7); send_byte(8'h7a); send_byte(1); send_byte(2);
        repeat(65) @(posedge clk);
        transaction(1,1,20,0,0,0,0,0);
        // Reject every SEND field boundary without starting the engine.
        test_payload[111:104]=4;
        transaction(1,2,21,14,0,2,0,0);
        test_payload[111:104]=0; test_payload[39:32]=27;
        transaction(1,2,22,14,0,2,0,0);
        test_payload[39:32]=1;
        transaction(1,2,23,14,0,2,0,0); // word does not fit
        test_payload[31:0]=1; test_payload[55:40]=0;
        transaction(1,2,24,14,0,2,0,0);
        test_payload[55:40]=1; test_payload[71:56]=0;
        transaction(1,2,25,14,0,2,0,0);
        // repeat_count 0 is continuous emission: still running after many
        // frames, BUSY to a second SEND, and ended only by STOP.
        test_payload[71:56]=1; test_payload[103:88]=0;
        transaction(1,2,26,14,0,0,1,0);
        repeat(300) @(posedge clk);
        transaction(1,4,60,0,0,0,1,-1);
        if(!busy || completed==0) $fatal(1,"Continuous SEND ended by itself");
        transaction(1,2,61,14,0,3,1,-1);
        transaction(1,3,62,0,0,0,0,-1);
        if(busy || dr || df || cr || cf || lr || lf)
            $fatal(1,"STOP did not end continuous emission");
        stopped_completed=completed;
        transaction(1,4,63,0,0,0,0,stopped_completed);
        // One-tick latch/gap zero and finite completion.
        test_payload[103:88]=2; test_payload[87:72]=0;
        transaction(1,2,27,14,0,0,1,0);
        transaction(1,4,28,0,0,0,0,2);
        // LED: {manual, 3'b0, pattern}; reserved bits or a wrong length are
        // rejected without a write. Automatic mode is a write with bit 4 clear.
        test_payload[7:0]=8'h85;
        transaction(1,5,40,1,0,0,0,-1);
        if(led_writes!=1 || last_led!==5'b10101) $fatal(1,"LED manual write missing");
        test_payload[7:0]=8'h15;
        transaction(1,5,41,1,0,2,0,-1);
        transaction(1,5,42,0,0,2,0,-1);
        transaction(1,5,43,2,0,2,0,-1);
        if(led_writes!=1) $fatal(1,"Invalid LED command reached the LEDs");
        test_payload[7:0]=8'h0a;
        transaction(1,5,44,1,0,0,0,-1);
        if(led_writes!=2 || last_led!==5'b01010) $fatal(1,"LED automatic write missing");
        // INFO pages carry revision, CORE_HZ and BUILD_ID in the 16-bit field.
        info_expected[0]=16'd3; info_expected[1]=16'hd180; info_expected[2]=16'h08f0;
        info_expected[3]=16'h0007; info_expected[4]=16'h1e2d; info_expected[5]=16'ha5c3;
        for(page=0;page<6;page=page+1) begin
            test_payload[7:0]=page;
            transaction(1,6,50+page,1,0,0,0,info_expected[page]);
        end
        test_payload[7:0]=6;
        transaction(1,6,56,1,0,2,0,-1);
        transaction(1,6,57,0,0,2,0,-1);
        // INFO and LED leave the completion counter untouched.
        transaction(1,4,58,0,0,0,0,2);
        // Reply TX backpressure and four-entry queue, with PING kept available.
        tx_ready=0;
        request(1,1,29,0,0);
        request(1,4,30,0,0);
        tx_ready=1;
        check_reply(captured_count,1,29,0,0,2);
        check_reply(captured_count,4,30,0,0,2);
        $display("PASS tb_protocol: %0d request/response cases",requests);
        $finish;
    end
    initial begin #1000000; $fatal(1,"Timeout"); end
endmodule
