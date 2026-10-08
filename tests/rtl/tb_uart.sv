`timescale 1ns/1ps
module tb_uart;
    reg clk=0;
    always #5 clk=~clk;
    reg reset=1;
    reg [7:0] input_data=0;
    reg input_valid=0;
    wire input_ready, serial;
    wire [7:0] output_data;
    wire output_valid;
    reg [7:0] expected[0:7];
    integer received=0, index;
    // Production oscillator/baud values, rather than only a scaled UART model.
    uart_tx transmitter(.clk(clk),.reset(reset),.data(input_data),
        .valid(input_valid),.ready(input_ready),.tx(serial));
    uart_rx receiver(.clk(clk),.reset(reset),.rx(serial),
        .data(output_data),.valid(output_valid));
    always @(posedge clk) if(output_valid) begin
        if(output_data!==expected[received])
            $fatal(1,"UART received %h expected %h",output_data,expected[received]);
        received=received+1;
    end
    initial begin
        expected[0]=8'h00; expected[1]=8'hff; expected[2]=8'ha7; expected[3]=8'h7a;
        expected[4]=8'h55; expected[5]=8'haa; expected[6]=8'h01; expected[7]=8'h80;
        repeat(4) @(posedge clk);
        @(negedge clk); reset=0;
        for(index=0;index<8;index=index+1) begin
            wait(input_ready);
            @(negedge clk); input_data=expected[index]; input_valid=1;
            @(negedge clk); input_valid=0;
        end
        wait(received==8);
        repeat(2000) @(posedge clk);
        if(received!=8) $fatal(1,"UART emitted extra bytes");
        $display("PASS tb_uart: production 100 MHz control clock / 115200 baud loopback");
        $finish;
    end
    initial begin #2000000; $fatal(1,"Timeout"); end
endmodule
