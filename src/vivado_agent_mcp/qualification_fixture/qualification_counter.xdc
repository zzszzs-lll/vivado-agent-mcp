set_property PACKAGE_PIN W5 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
create_clock -period 10.000 -name sys_clk [get_ports clk]

set_property PACKAGE_PIN U18 [get_ports rst_n]
set_property IOSTANDARD LVCMOS33 [get_ports rst_n]

set_property PACKAGE_PIN U16 [get_ports {count[0]}]
set_property PACKAGE_PIN E19 [get_ports {count[1]}]
set_property PACKAGE_PIN U19 [get_ports {count[2]}]
set_property PACKAGE_PIN V19 [get_ports {count[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {count[*]}]

set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
