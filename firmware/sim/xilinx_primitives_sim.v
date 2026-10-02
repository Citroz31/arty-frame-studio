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
// Same public parameter/port interface as Yosys' xc7 PLLE2_ADV declaration.
// Only the fixed CLKIN1, INTERNAL, DRP-disabled board configuration is modeled.
module PLLE2_ADV #(
    parameter BANDWIDTH="OPTIMIZED", parameter COMPENSATION="ZHOLD",
    parameter STARTUP_WAIT="FALSE",
    parameter integer CLKOUT0_DIVIDE=1, CLKOUT1_DIVIDE=1, CLKOUT2_DIVIDE=1,
    parameter integer CLKOUT3_DIVIDE=1, CLKOUT4_DIVIDE=1, CLKOUT5_DIVIDE=1,
    parameter integer DIVCLK_DIVIDE=1, CLKFBOUT_MULT=5,
    parameter real CLKFBOUT_PHASE=0.000,
    parameter real CLKIN1_PERIOD=0.000, CLKIN2_PERIOD=0.000,
    parameter real CLKOUT0_DUTY_CYCLE=0.500, CLKOUT0_PHASE=0.000,
    parameter real CLKOUT1_DUTY_CYCLE=0.500, CLKOUT1_PHASE=0.000,
    parameter real CLKOUT2_DUTY_CYCLE=0.500, CLKOUT2_PHASE=0.000,
    parameter real CLKOUT3_DUTY_CYCLE=0.500, CLKOUT3_PHASE=0.000,
    parameter real CLKOUT4_DUTY_CYCLE=0.500, CLKOUT4_PHASE=0.000,
    parameter real CLKOUT5_DUTY_CYCLE=0.500, CLKOUT5_PHASE=0.000,
    parameter [0:0] IS_CLKINSEL_INVERTED=1'b0,
    parameter [0:0] IS_PWRDWN_INVERTED=1'b0, IS_RST_INVERTED=1'b0,
    parameter real REF_JITTER1=0.010, REF_JITTER2=0.010,
    parameter real VCOCLK_FREQ_MAX=2133.000, VCOCLK_FREQ_MIN=800.000,
    parameter real CLKIN_FREQ_MAX=1066.000, CLKIN_FREQ_MIN=19.000,
    parameter real CLKPFD_FREQ_MAX=550.0, CLKPFD_FREQ_MIN=19.0
) (
    input wire CLKIN1,CLKIN2,CLKINSEL,CLKFBIN,RST,PWRDWN,DCLK,DEN,DWE,
    input wire [15:0] DI,
    input wire [6:0] DADDR,
    output wire CLKFBOUT,CLKOUT0,CLKOUT1,CLKOUT2,CLKOUT3,CLKOUT4,CLKOUT5,
    output wire LOCKED,DRDY,
    output wire [15:0] DO
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
    assign DRDY=0;
    assign DO=0;
    initial begin
        if(COMPENSATION!="INTERNAL" || CLKIN1_PERIOD<=0.0
            || DIVCLK_DIVIDE!=1 || CLKFBOUT_MULT!=10 || CLKOUT0_DIVIDE!=5
            || CLKFBOUT_PHASE!=0.0 || CLKOUT0_PHASE!=0.0
            || CLKOUT0_DUTY_CYCLE!=0.5 || IS_CLKINSEL_INVERTED
            || IS_PWRDWN_INVERTED || IS_RST_INVERTED)
            $fatal(1,"PLL model supports only the board's fixed 100-to-200 MHz INTERNAL mode");
        #0;
        if(CLKINSEL!==1'b1 || CLKIN2!==1'b0 || DCLK!==1'b0
            || DEN!==1'b0 || DWE!==1'b0 || DI!==16'b0 || DADDR!==7'b0)
            $fatal(1,"PLL model requires CLKIN1 selected and DRP inputs disabled");
    end
    always @(posedge DCLK or posedge DEN or posedge DWE or negedge CLKINSEL)
        $fatal(1,"PLL model does not support DRP or clock input switching");
endmodule
