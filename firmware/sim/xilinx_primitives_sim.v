`timescale 1ns/1ps
// Behavioral models for test elaboration only. They do not model device timing,
// clock jitter, physical routing or all primitive modes. Never synthesize this.
module IBUF(input wire I,output wire O); assign O=I; endmodule
module BUFG(input wire I,output wire O); assign O=I; endmodule
module ODDR #(
    parameter DDR_CLK_EDGE="SAME_EDGE", parameter INIT=1'b0,
    parameter SRTYPE="ASYNC"
) (
    input wire C,CE,D1,D2,R,S,output reg Q=INIT
);
    reg saved_d2=INIT;
    always @(posedge C or negedge C or posedge R or posedge S) begin
        if(R) begin Q<=0; saved_d2<=0; end
        else if(S) begin Q<=1; saved_d2<=1; end
        else if(CE) begin
            if(C) begin Q<=D1; saved_d2<=D2; end
            else Q<=saved_d2;
        end
    end
    initial if(DDR_CLK_EDGE!="SAME_EDGE" || SRTYPE!="ASYNC")
        $fatal(1,"Primitive model supports only SAME_EDGE ASYNC");
endmodule
module PLLE2_BASE #(
    parameter BANDWIDTH="OPTIMIZED", parameter COMPENSATION="INTERNAL",
    parameter real CLKIN1_PERIOD=10.0,
    parameter integer DIVCLK_DIVIDE=1, parameter integer CLKFBOUT_MULT=10,
    parameter integer CLKOUT0_DIVIDE=5, parameter STARTUP_WAIT="FALSE"
) (
    input wire CLKIN1,CLKFBIN,RST,PWRDWN,
    output wire CLKFBOUT,CLKOUT0,CLKOUT1,CLKOUT2,CLKOUT3,CLKOUT4,CLKOUT5,
    output wire LOCKED
);
    localparam real HALF_PERIOD=CLKIN1_PERIOD*DIVCLK_DIVIDE*CLKOUT0_DIVIDE
                               /(2.0*CLKFBOUT_MULT);
    reg oscillator=0;
    reg [3:0] lock_count=0;
    always #(HALF_PERIOD) oscillator=~oscillator;
    always @(posedge CLKIN1 or posedge RST or posedge PWRDWN)
        if(RST || PWRDWN) lock_count<=0;
        else if(lock_count!=15) lock_count<=lock_count+1'b1;
    assign LOCKED=lock_count==15 && !RST && !PWRDWN;
    assign CLKOUT0=(!RST && !PWRDWN) ? oscillator : 0;
    assign CLKFBOUT=CLKIN1;
    assign {CLKOUT1,CLKOUT2,CLKOUT3,CLKOUT4,CLKOUT5}=0;
endmodule
