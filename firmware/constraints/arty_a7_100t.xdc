# Arty A7-100T xc7a100tcsg324-1, board revisions D/E.
# Digilent: https://github.com/Digilent/digilent-xdc/blob/master/Arty-A7-100-Master.xdc
# Only nextpnr-xilinx-supported set_property/create_clock commands are used.
# The second create_clock explicitly constrains the synthesized 200 MHz BUFG
# net. create_generated_clock is NOT implemented by this nextpnr XDC parser.
set_property -dict {PACKAGE_PIN E3 IOSTANDARD LVCMOS33} [get_ports clk100]
create_clock -period 10.000 -name clk100 [get_ports clk100]
create_clock -period 5.000 -name core_clock [get_nets core_clock]

# ck_rst: red RESET button, active low (not the user push buttons).
set_property -dict {PACKAGE_PIN C2 IOSTANDARD LVCMOS33} [get_ports reset_n]

# FT2232 USB-UART: rxd_out is an INPUT to FPGA, txd_in an OUTPUT.
set_property -dict {PACKAGE_PIN D10 IOSTANDARD LVCMOS33} [get_ports uart_rx]
set_property -dict {PACKAGE_PIN A9 IOSTANDARD LVCMOS33} [get_ports uart_tx]

# JB physical pins 1, 2, 3 (high-speed Pmod). Ground: JB5/JB11.
# JB6/JB12 are 3.3 V supply. JB differential pairs are used as single-ended IO.
set_property -dict {PACKAGE_PIN E15 IOSTANDARD LVCMOS33 SLEW FAST DRIVE 8} [get_ports data_out]
set_property -dict {PACKAGE_PIN E16 IOSTANDARD LVCMOS33 SLEW FAST DRIVE 8} [get_ports frame_clk]
set_property -dict {PACKAGE_PIN D15 IOSTANDARD LVCMOS33 SLEW FAST DRIVE 8} [get_ports latch_enable]

# Four monochrome LEDs: locked, busy, reserved, completed (LSB first).
set_property -dict {PACKAGE_PIN H5 IOSTANDARD LVCMOS33} [get_ports {led[0]}]
set_property -dict {PACKAGE_PIN J5 IOSTANDARD LVCMOS33} [get_ports {led[1]}]
set_property -dict {PACKAGE_PIN T9 IOSTANDARD LVCMOS33} [get_ports {led[2]}]
set_property -dict {PACKAGE_PIN T10 IOSTANDARD LVCMOS33} [get_ports {led[3]}]
