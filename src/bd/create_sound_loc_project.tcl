#############################################################################
#  create_project.tcl  ——  四麦克风声源定位装置  Vivado 2026.1 一键建工程
#
#  【它替你做完这些事】（你只需要等）
#     1. 新建工程          <QZ_BUILD_DIR>/prj
#     2. 加入 RTL 源码     rtl 目录下全部 .v（自动 glob，本设计 8 个文件）
#     3. 加入引脚约束     constr/sound_loc_four_mic.xdc（提交包）或 xdc/（源码树）
#     4. 搭好 Block Design  system.bd
#          · Zynq PS（复用你 GPIO_MIO 工程里那套配置：DDR + UART1）
#          · AXI GPIO     —— PS 与 PL 的握手（读 wr_cnt / 写 rd_cnt）
#          · AXI BRAM Ctrl + Block Memory Generator —— 64 位 2048 深的乒乓缓冲
#          · AXI Interconnect —— 把 PS 的 M_AXI_GP0 分成两路
#          · acq_top 直接作为模块加进 BD（不封装 IP）
#     5. 生成 HDL Wrapper 并设为顶层
#     6. 综合 → 实现 → 生成 bitstream → 导出 .xsa（给 Vitis 用）
#
#  【怎么用】
#     方式一：打开 Vivado，菜单 Tools -> Run Tcl Script...，选本文件
#     方式二：命令行  vivado -mode batch -source create_project.tcl
#
#  【产物】（都落在 QZ_BUILD_DIR 下，默认 ../quietzone_build/sound_loc_final）
#     bit ： <QZ_BUILD_DIR>/sound_loc_integrated.bit
#     xsa ： <QZ_BUILD_DIR>/sound_loc_integrated.xsa      ← Vitis 建平台用这个
#     rpt ： <QZ_BUILD_DIR>/reports/*.rpt
#############################################################################

set proj_name "sound_loc_integrated"
set script_dir [file normalize [file dirname [info script]]]
set root_dir   [file normalize "$script_dir/.."]

# 目录布局自适应：本脚本同时适用于源码树（quietzone/rtl 等）与报名提交包（src/rtl 等）
if {[file isdirectory "$root_dir/rtl/sound_loc"]} {
    set rtl_dir "$root_dir/rtl/sound_loc"
    set tb_dir  "$root_dir/tb"
    set xdc_dir "$root_dir/xdc"
} else {
    set rtl_dir "$root_dir/rtl"
    set tb_dir  [file normalize "$root_dir/../sim/tb"]
    set xdc_dir "$root_dir/constr"
}

if {[info exists ::env(QZ_BUILD_DIR)]} {
    set build_dir [file normalize $::env(QZ_BUILD_DIR)]
} else {
    set build_dir [file normalize "$root_dir/../quietzone_build/sound_loc_final"]
}
set prj_dir   "$build_dir/prj"
set report_dir "$build_dir/reports"
file mkdir $build_dir
file mkdir $report_dir
set part      "xc7z020clg400-2"

# 调试用：设成 bd 就只搭到 Block Design 校验完就停（我自己验证时用）
set stage "all"
if {[info exists ::env(SL_STAGE)]} { set stage $::env(SL_STAGE) }

# 并行 job 数：默认 1（与验机时一致，最稳），可用 QZ_JOBS 覆盖
set jobs 1
if {[info exists ::env(QZ_JOBS)]} { set jobs $::env(QZ_JOBS) }

puts "########## 阶段 = $stage ##########"

#=============================================================================
# 1. 建工程
#=============================================================================
catch { close_project }      ;# 若 Vivado 里还开着别的工程，先关掉，避免 prj 被锁
create_project $proj_name $prj_dir -part $part -force
puts "  + 工程已建立：$prj_dir"

#=============================================================================
# 2. 加源码
#=============================================================================
set src_files [glob -nocomplain -directory $rtl_dir *.v]
if {[llength $src_files] == 0} {
    puts "ERROR: $rtl_dir 下没有 .v 文件，路径对不上。"
    return
}
add_files -fileset sources_1 -norecurse $src_files
foreach f $src_files { puts "  + 源码 $f" }

# 加仿真平台（仿真专用，不参与综合）
set sim_files [glob -nocomplain -directory $tb_dir tb_*.v]
if {[llength $sim_files] > 0 && [file exists [lindex $sim_files 0]]} {
    add_files -fileset sim_1 -norecurse $sim_files
    # 把测试平台设为仿真顶层：这样在 Vivado 里点 Run Simulation 就能直接跑
    set_property top tb_acq_top [get_filesets sim_1]
    set_property top_lib xil_defaultlib [get_filesets sim_1]
    update_compile_order -fileset sim_1
    foreach f $sim_files { puts "  + 仿真 $f" }
}
update_compile_order -fileset sources_1

#=============================================================================
# 3. 加引脚约束
#=============================================================================
set xdc_file "$xdc_dir/sound_loc_four_mic.xdc"
if {![file exists $xdc_file]} { puts "ERROR: 找不到 $xdc_file"; return }
add_files -fileset constrs_1 -norecurse $xdc_file
puts "  + 约束 $xdc_file"

#=============================================================================
# 4. 搭 Block Design
#=============================================================================
create_bd_design "system"
puts "  + Block Design system 已创建"

#--------------------------------------------- 4.1 Zynq 处理器系统 (PS7)
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0
set_property -dict [list \
    CONFIG.PCW_UIPARAM_DDR_PARTNO        {MT41J256M16 RE-125} \
    CONFIG.PCW_DDR_RAM_HIGHADDR          {0x3FFFFFFF} \
    CONFIG.PCW_UART1_PERIPHERAL_ENABLE   {1} \
    CONFIG.PCW_UART1_UART1_IO            {MIO 48 .. 49} \
    CONFIG.PCW_EN_UART1                  {1} \
    CONFIG.PCW_UART_PERIPHERAL_FREQMHZ   {100} \
    CONFIG.PCW_UART_PERIPHERAL_VALID     {1} \
    CONFIG.PCW_EN_GPIO                   {1} \
    CONFIG.PCW_GPIO_MIO_GPIO_ENABLE      {1} \
    CONFIG.PCW_GPIO_MIO_GPIO_IO          {MIO} \
    CONFIG.PCW_SD0_PERIPHERAL_ENABLE    {1} \
    CONFIG.PCW_SD0_SD0_IO               {MIO 40 .. 45} \
    CONFIG.PCW_SD0_GRP_CD_ENABLE        {0} \
    CONFIG.PCW_SD0_GRP_WP_ENABLE        {0} \
    CONFIG.PCW_SD0_GRP_POW_ENABLE       {0} \
    CONFIG.PCW_I2C_RESET_ENABLE          {1} \
    CONFIG.PCW_USB_RESET_ENABLE          {1} \
    CONFIG.PCW_USE_M_AXI_GP0             {1} \
    CONFIG.PCW_EN_CLK0_PORT              {1} \
    CONFIG.PCW_EN_RST0_PORT              {1} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ  {50} \
] [get_bd_cells processing_system7_0]
puts "  + PS7 配置完成（DDR3 + UART1/MIO48-49 + SD0/MIO40-45,CD disabled + M_AXI_GP0 + FCLK0=50MHz）"

#--------------------------------------------- 4.2 复位管理
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_ps7_50M
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 const_1
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {1}] [get_bd_cells const_1]
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 const_0
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {0}] [get_bd_cells const_0]

#--------------------------------------------- 4.3 AXI 互联（1 主 2 从）
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 axi_interconnect_0
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {3}] [get_bd_cells axi_interconnect_0]

#--------------------------------------------- 4.4 AXI GPIO（握手）
#   通道1：全输入，位宽 2   ← PL 的 wr_cnt 送进来，PS 读
#   通道2：全输出，位宽 2   → PS 写 rd_cnt，送给 PL
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 axi_gpio_0
set_property -dict [list \
    CONFIG.C_IS_DUAL        {1} \
    CONFIG.C_GPIO_WIDTH     {2} \
    CONFIG.C_ALL_INPUTS     {1} \
    CONFIG.C_TRI_DEFAULT    {0xFFFFFFFF} \
    CONFIG.C_GPIO2_WIDTH    {2} \
    CONFIG.C_ALL_OUTPUTS_2  {1} \
    CONFIG.C_DOUT_DEFAULT_2 {0x0} \
] [get_bd_cells axi_gpio_0]

#--------------------------------------------- 4.4b AXI GPIO（屏幕/灯环控制）
#   双通道各 32 位全输出 -> 通道1: fill/state/db10；通道2: led_color/led_update
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 axi_gpio_1
set_property -dict [list \
    CONFIG.C_IS_DUAL         {1} \
    CONFIG.C_GPIO_WIDTH      {32} \
    CONFIG.C_ALL_OUTPUTS     {1} \
    CONFIG.C_GPIO2_WIDTH     {32} \
    CONFIG.C_ALL_OUTPUTS_2   {1} \
    CONFIG.C_DOUT_DEFAULT    {0x00000000} \
    CONFIG.C_DOUT_DEFAULT_2  {0x00000000} \
] [get_bd_cells axi_gpio_1]

#--------------------------------------------- 4.5 BRAM 控制器 + 双口 BRAM
#   注意两个坑（2026.1 实测）：
#     ① axi_bram_ctrl 的 MEM_DEPTH 是只读参数，深度由"地址范围 ÷ 数据宽度"自动算；
#     ② BMG 里激活 BRAM 控制器接口的参数叫 use_bram_block，
#        而不是 Interface_Type（后者只剩 Native 可选）。
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl:4.1 axi_bram_ctrl_0
set_property -dict [list \
    CONFIG.SINGLE_PORT_BRAM {1} \
    CONFIG.DATA_WIDTH       {64} \
    CONFIG.ECC_TYPE         {0} \
] [get_bd_cells axi_bram_ctrl_0]

create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen:8.4 blk_mem_gen_0
set_property -dict [list \
    CONFIG.Memory_Type        {True_Dual_Port_RAM} \
    CONFIG.use_bram_block     {BRAM_Controller} \
    CONFIG.Write_Width_A      {64} \
    CONFIG.Read_Width_A       {64} \
    CONFIG.Write_Width_B      {64} \
    CONFIG.Read_Width_B       {64} \
    CONFIG.Enable_B           {Use_ENB_Pin} \
    CONFIG.Register_PortB_Output_of_Memory_Primitives {false} \
    CONFIG.Load_Init_File     {false} \
] [get_bd_cells blk_mem_gen_0]

#--------------------------------------------- 4.6 把 acq_top 作为模块加进 BD
if {[catch {create_bd_cell -type module -reference acq_top acq_top_0} err]} {
    puts "ERROR: 把 acq_top 加进 BD 失败：$err"
    return
}
puts "  + acq_top 已作为模块加入 BD"

if {[catch {create_bd_cell -type module -reference lcd_led_top lcd_led_top_0} err]} {
    puts "ERROR: 把 lcd_led_top 加进 BD 失败：$err"
    return
}
puts "  + lcd_led_top 已作为模块加入 BD"
puts "  + [get_bd_pins -quiet -of_objects [get_bd_cells acq_top_0]]"
puts "  + GPIO引脚: [get_bd_pins -quiet -of_objects [get_bd_cells axi_gpio_0]]"

# 外部端口：50MHz 时钟 + 麦克风 6 根线
create_bd_port -dir I -type clk -freq_hz 50000000 clk_50m
create_bd_port -dir O mic_sck
create_bd_port -dir O mic_ws
foreach n {mic_sd0 mic_sd1 mic_sd2 mic_sd3} { create_bd_port -dir I $n }
puts "  + 外部端口已创建（clk_50m / mic_sck / mic_ws / mic_sd0-3）"
create_bd_port -dir O lcd_scl
create_bd_port -dir O lcd_sda
create_bd_port -dir O lcd_dc
create_bd_port -dir O lcd_cs
create_bd_port -dir O lcd_rst
create_bd_port -dir O lcd_blk
create_bd_port -dir O ws2812_din

#=============================================================================
# 5. 连线
#=============================================================================
#--- 5.1 AXI 时钟网络：FCLK_CLK0 驱动一切 AXI 侧逻辑
set ps_clk  [get_bd_pins processing_system7_0/FCLK_CLK0]
set ps_rstn [get_bd_pins processing_system7_0/FCLK_RESET0_N]

connect_bd_net $ps_clk \
    [get_bd_pins processing_system7_0/M_AXI_GP0_ACLK] \
    [get_bd_pins rst_ps7_50M/slowest_sync_clk] \
    [get_bd_pins axi_interconnect_0/ACLK] \
    [get_bd_pins axi_interconnect_0/S00_ACLK] \
    [get_bd_pins axi_interconnect_0/M00_ACLK] \
    [get_bd_pins axi_interconnect_0/M01_ACLK] \
    [get_bd_pins axi_gpio_0/s_axi_aclk] \
    [get_bd_pins axi_interconnect_0/M02_ACLK] \
    [get_bd_pins axi_gpio_1/s_axi_aclk] \
    [get_bd_pins axi_bram_ctrl_0/s_axi_aclk]

connect_bd_net $ps_rstn [get_bd_pins rst_ps7_50M/ext_reset_in]
connect_bd_net [get_bd_pins const_1/dout] \
    [get_bd_pins rst_ps7_50M/aux_reset_in] \
    [get_bd_pins rst_ps7_50M/dcm_locked]
connect_bd_net [get_bd_pins const_0/dout] [get_bd_pins rst_ps7_50M/mb_debug_sys_rst]

set ps_aresetn [get_bd_pins rst_ps7_50M/peripheral_aresetn]
connect_bd_net $ps_aresetn \
    [get_bd_pins axi_interconnect_0/ARESETN] \
    [get_bd_pins axi_interconnect_0/S00_ARESETN] \
    [get_bd_pins axi_interconnect_0/M00_ARESETN] \
    [get_bd_pins axi_interconnect_0/M01_ARESETN] \
    [get_bd_pins axi_gpio_0/s_axi_aresetn] \
    [get_bd_pins axi_interconnect_0/M02_ARESETN] \
    [get_bd_pins axi_gpio_1/s_axi_aresetn] \
    [get_bd_pins axi_bram_ctrl_0/s_axi_aresetn]

#--- 5.2 AXI 数据通路
connect_bd_intf_net [get_bd_intf_pins processing_system7_0/M_AXI_GP0] [get_bd_intf_pins axi_interconnect_0/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_interconnect_0/M00_AXI]     [get_bd_intf_pins axi_bram_ctrl_0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_interconnect_0/M01_AXI]     [get_bd_intf_pins axi_gpio_0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_interconnect_0/M02_AXI]     [get_bd_intf_pins axi_gpio_1/S_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_bram_ctrl_0/BRAM_PORTA]     [get_bd_intf_pins blk_mem_gen_0/BRAM_PORTA]

#--- 5.3 50MHz 时钟域：acq_top 与 BRAM Port B
#      BMG 在"BRAM 控制器"模式下，Port B 的引脚名是小写：
#        clkb / addrb / dinb / enb / web / rstb
connect_bd_net [get_bd_ports clk_50m] [get_bd_pins acq_top_0/clk_50m]
connect_bd_net [get_bd_pins acq_top_0/bram_clk] [get_bd_pins blk_mem_gen_0/clkb]

connect_bd_net [get_bd_ports mic_sck] [get_bd_pins acq_top_0/mic_sck]
connect_bd_net [get_bd_ports mic_ws]  [get_bd_pins acq_top_0/mic_ws]
foreach n {mic_sd0 mic_sd1 mic_sd2 mic_sd3} {
    connect_bd_net [get_bd_ports $n] [get_bd_pins acq_top_0/$n]
}

#--- 5.4 PL 写 BRAM Port B
connect_bd_net [get_bd_pins acq_top_0/bram_addr] [get_bd_pins blk_mem_gen_0/addrb]
connect_bd_net [get_bd_pins acq_top_0/bram_din]  [get_bd_pins blk_mem_gen_0/dinb]
connect_bd_net [get_bd_pins acq_top_0/bram_we]   [get_bd_pins blk_mem_gen_0/web]
connect_bd_net [get_bd_pins acq_top_0/bram_en]   [get_bd_pins blk_mem_gen_0/enb]
connect_bd_net [get_bd_pins acq_top_0/bram_rst]  [get_bd_pins blk_mem_gen_0/rstb]

#--- 5.5 握手（跨时钟域的两条账本信号）
#     注意：双通道 AXI GPIO 的通道2 引脚名带 2 —— gpio2_io_o
connect_bd_net [get_bd_pins acq_top_0/wr_cnt_ps] [get_bd_pins axi_gpio_0/gpio_io_i]
connect_bd_net [get_bd_pins axi_gpio_0/gpio2_io_o] [get_bd_pins acq_top_0/rd_cnt_ps]
connect_bd_net [get_bd_pins axi_gpio_1/gpio_io_o]  [get_bd_pins lcd_led_top_0/ctrl1]
connect_bd_net [get_bd_pins axi_gpio_1/gpio2_io_o] [get_bd_pins lcd_led_top_0/ctrl2]
connect_bd_net [get_bd_ports clk_50m] [get_bd_pins lcd_led_top_0/clk_50m]
connect_bd_net [get_bd_pins rst_ps7_50M/peripheral_aresetn] [get_bd_pins lcd_led_top_0/rst_n]
connect_bd_net [get_bd_pins lcd_led_top_0/lcd_scl] [get_bd_ports lcd_scl]
connect_bd_net [get_bd_pins lcd_led_top_0/lcd_sda] [get_bd_ports lcd_sda]
connect_bd_net [get_bd_pins lcd_led_top_0/lcd_dc]  [get_bd_ports lcd_dc]
connect_bd_net [get_bd_pins lcd_led_top_0/lcd_cs]  [get_bd_ports lcd_cs]
connect_bd_net [get_bd_pins lcd_led_top_0/lcd_rst] [get_bd_ports lcd_rst]
connect_bd_net [get_bd_pins lcd_led_top_0/lcd_blk] [get_bd_ports lcd_blk]
connect_bd_net [get_bd_pins lcd_led_top_0/ws2812_din] [get_bd_ports ws2812_din]

#=============================================================================
# 6. 地址分配
#    BRAM → 0x4000_0000（16 KB，正好是 2048 × 64bit）
#    GPIO → 0x4120_0000（Vivado 惯例，PS 代码里用 XPAR 宏，不手写这个地址）
#=============================================================================
assign_bd_address -target_address_space /processing_system7_0/Data \
    [get_bd_addr_segs {axi_bram_ctrl_0/S_AXI/Mem0}] -force \
    -offset 0x40000000 -range 16K
assign_bd_address -target_address_space /processing_system7_0/Data \
    [get_bd_addr_segs {axi_gpio_0/S_AXI/Reg}] -force \
    -offset 0x41200000 -range 64K
assign_bd_address -target_address_space /processing_system7_0/Data \
    [get_bd_addr_segs {axi_gpio_1/S_AXI/Reg}] -force \
    -offset 0x41210000 -range 64K
puts "  + 地址分配完成：BRAM=0x4000_0000(16K)  GPIO=0x4120_0000(64K)"

#=============================================================================
# 7. 校验
#=============================================================================
regenerate_bd_layout
if {[catch {validate_bd_design} err]} {
    puts "!!!!!! Block Design 校验失败 !!!!!!"
    puts $err
    return
}
save_bd_design
puts "  + Block Design 校验通过"
puts "  + BRAM 实际配置：AXI侧 DATA_WIDTH=[get_property CONFIG.DATA_WIDTH [get_bd_cells axi_bram_ctrl_0]]bit  MEM_DEPTH(字)=[get_property CONFIG.MEM_DEPTH [get_bd_cells axi_bram_ctrl_0]]  BMG深度=[get_property CONFIG.Write_Depth_A [get_bd_cells blk_mem_gen_0]]"
puts "  + 地址段："
foreach seg [get_bd_addr_segs -quiet] {
    puts "      [get_property NAME $seg]  offset=[get_property OFFSET $seg]  range=[get_property RANGE $seg]"
}

# --- 打印一份连线清单，方便肉眼核对
puts "==== BD 端口 ===="
foreach p [get_bd_ports -quiet] { puts "   PORT $p" }
puts "==== BD 连线 ===="
foreach n [get_bd_nets -quiet] {
    set pins [get_bd_pins -quiet -of_objects $n]
    if {[llength $pins] > 0} {
        puts "   NET [get_property NAME $n]  ->  [get_property NAME $pins]"
    }
}

if {$stage eq "bd"} {
    puts "########## 停在 Block Design（SL_STAGE=bd） ##########"
    return
}

#=============================================================================
# 8. 生成 HDL Wrapper 并设为顶层
#=============================================================================
set bd_file [get_files system.bd]
if {[llength $bd_file] == 0} { set bd_file [get_files */system.bd] }
make_wrapper -files $bd_file -top
set wrap_files [glob -nocomplain "$prj_dir/$proj_name.gen/sources_1/bd/system/hdl/system_wrapper.v"]
if {[llength $wrap_files] == 0} { puts "ERROR: wrapper 没生成出来"; return }
add_files -norecurse $wrap_files
set_property top system_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1
puts "  + 顶层已设为 system_wrapper"

#=============================================================================
# 9. 综合
#=============================================================================
puts "==================== 开始综合 ===================="
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    puts "ERROR: 综合失败，看 Messages 面板。"
    return
}
puts "==================== 综合完成 ===================="

if {$stage eq "synth"} {
    puts "########## 停在综合（SL_STAGE=synth） ##########"
    return
}

#=============================================================================
# 10. 实现 + 生成 bitstream
#=============================================================================
puts "==================== 开始实现 ===================="
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    puts "ERROR: 实现失败，看 Messages 面板。"
    return
}
puts "==================== 实现完成 ===================="

#=============================================================================
# 11. 导出 .xsa（Vitis 建平台用）
#=============================================================================
set xsa "$build_dir/sound_loc_integrated.xsa"
write_hw_platform -fixed -include_bit -force $xsa
puts "  + 已导出 $xsa"

puts ""
set timing_rpt "$report_dir/timing_summary_routed.rpt"
set util_rpt   "$report_dir/utilization_routed.rpt"
set drc_rpt    "$report_dir/drc_routed.rpt"
open_run impl_1
report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose -max_paths 20 -file $timing_rpt
report_utilization -hierarchical -file $util_rpt
report_drc -file $drc_rpt

#-----------------------------------------------------------------------------
# 12. 把位流复制到 <QZ_BUILD_DIR>/sound_loc_integrated.bit（与报告、XSA 放一起）
#-----------------------------------------------------------------------------
set bit_list [glob -nocomplain "$prj_dir/$proj_name.runs/impl_1/*.bit"]
if {[llength $bit_list] > 0} {
    set bit_src [lindex [lsort -decreasing $bit_list] 0]
    file copy -force $bit_src "$build_dir/sound_loc_integrated.bit"
    puts "  + 位流已复制到 $build_dir/sound_loc_integrated.bit"
} else {
    puts "ERROR: impl_1 没有产出 .bit"
}

puts "################ 全部完成 ################"
puts "bit 文件： $build_dir/sound_loc_integrated.bit"
puts "xsa 文件： $xsa"
puts "时序报告： $timing_rpt"
puts "资源报告： $util_rpt"
puts "DRC 报告： $drc_rpt"
puts "##########################################"
