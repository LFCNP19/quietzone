#=============================================================================
#  sound_loc.xdc  ——  四麦克风声源定位装置
#  开发板：黑金 AX7020（XC7Z020-CLG400-2）
#  软件  ：Vivado 2026.1
#
#  ⚠️ 端口名说明
#    如果你是按"Add Module 把 acq_top 加进 Block Design"的方式搭建的，
#    那么最终顶层是 Vivado 自动生成的 design_1_wrapper，
#    端口名一般会保留原样（clk_50m / mic_sck / mic_ws / mic_sd0~3）。
#    如果综合时报 "no objects matched"，先跑一次
#        report_property [get_ports]
#    看实际端口名，把下面 7 处的名字改成真实名字即可。
#=============================================================================

#-----------------------------------------------------------------------------
# 1. 系统时钟 50 MHz（板载晶振 U18）
#-----------------------------------------------------------------------------
set_property PACKAGE_PIN U18 [get_ports clk_50m]
set_property IOSTANDARD  LVCMOS33 [get_ports clk_50m]
create_clock -period 20.000 -name sys_clk -waveform {0.000 10.000} [get_ports clk_50m]

#-----------------------------------------------------------------------------
# 2. 麦克风接口  →  J10 扩展口（40 pin 双排，IO 电平 3.3V）
#
#    ┌────────────┬──────────┬─────────────┬──────────────────────────┐
#    │ FPGA 引脚  │ J10 管脚 │ 信号        │ 说明                     │
#    ├────────────┼──────────┼─────────────┼──────────────────────────┤
#    │ W18        │ PIN4     │ mic_sck     │ 位时钟 3.125 MHz（4路共用）│
#    │ P14        │ PIN6     │ mic_ws      │ 帧同步 48.83 kHz（4路共用）│
#    │ Y16        │ PIN8     │ mic_sd0     │ 麦克风 0 的数据           │
#    │ V15        │ PIN10    │ mic_sd1     │ 麦克风 1 的数据           │
#    │ W14        │ PIN12    │ mic_sd2     │ 麦克风 2 的数据           │
#    │ N17        │ PIN14    │ mic_sd3     │ 麦克风 3 的数据           │
#    │ +3.3V      │ PIN39/40 │ VDD         │ 4 个麦克风并联供电        │
#    │ GND        │ PIN1/37/38 │ GND       │ 4 个麦克风共地            │
#    └────────────┴──────────┴─────────────┴──────────────────────────┘
#
#    ⚠️ 4 个 INMP441 的 L/R 引脚**必须全部接 GND**（左声道）。
#       四路都在左声道，采样时刻才严格同步 —— 这是算 TDOA 时延的前提。
#    ⚠️ 扩展口 IO 是 3.3V 电平，切勿接 5V 设备。
#    ⚠️ SCK 是 3.125 MHz 的信号，杜邦线尽量短（20 cm 以内），否则串扰。
#-----------------------------------------------------------------------------
set_property PACKAGE_PIN W18 [get_ports mic_sck]
set_property IOSTANDARD  LVCMOS33 [get_ports mic_sck]

set_property PACKAGE_PIN P14 [get_ports mic_ws]
set_property IOSTANDARD  LVCMOS33 [get_ports mic_ws]

set_property PACKAGE_PIN Y16 [get_ports mic_sd0]
set_property IOSTANDARD  LVCMOS33 [get_ports mic_sd0]

set_property PACKAGE_PIN V15 [get_ports mic_sd1]
set_property IOSTANDARD  LVCMOS33 [get_ports mic_sd1]

set_property PACKAGE_PIN W14 [get_ports mic_sd2]
set_property IOSTANDARD  LVCMOS33 [get_ports mic_sd2]

set_property PACKAGE_PIN N17 [get_ports mic_sd3]
set_property IOSTANDARD  LVCMOS33 [get_ports mic_sd3]

#-----------------------------------------------------------------------------
# 3. 时序例外
#
#    mic_sd0~3 是由 mic_sck（3.125 MHz）驱动的慢速同步信号，
#    而本设计的采样点由计数器 cnt 对齐（在 cnt[3:0]==7 这一刻取数），
#    并不依赖系统时钟边沿去直接抓，
#    所以这里声明为 false_path，免得工具报一堆无意义的建立/保持违例。
#
#    注意：这是"时序分析层面"的处理，和"逻辑上算不算同一个时钟域"是两件事。
#    mic_sck/mic_ws 由 cnt 分频而来，与 50 MHz 同源相位锁死，
#    逻辑上它们本来就属于同一个时钟域（所以不需要异步 FIFO）。
#-----------------------------------------------------------------------------
set_false_path -from [get_ports mic_sd0]
set_false_path -from [get_ports mic_sd1]
set_false_path -from [get_ports mic_sd2]
set_false_path -from [get_ports mic_sd3]

#-----------------------------------------------------------------------------
#-----------------------------------------------------------------------------
# 4. LCD（GC9A01 圆屏，PL 硬件 SPI 引擎）+ WS2812B 灯环
#    lcd_scl/sda/dc/cs/rst/blk 由 lcd_led_top 驱动，ws2812_din 由灯环驱动
#    全部 Bank34，LVCMOS33；与麦克风引脚（W18/P14/Y16/V15/W14/N17）不冲突
#-----------------------------------------------------------------------------
set_property PACKAGE_PIN U14 [get_ports lcd_scl]
set_property IOSTANDARD  LVCMOS33 [get_ports lcd_scl]

set_property PACKAGE_PIN P15 [get_ports lcd_sda]
set_property IOSTANDARD  LVCMOS33 [get_ports lcd_sda]

set_property PACKAGE_PIN T16 [get_ports lcd_dc]
set_property IOSTANDARD  LVCMOS33 [get_ports lcd_dc]

set_property PACKAGE_PIN V17 [get_ports lcd_cs]
set_property IOSTANDARD  LVCMOS33 [get_ports lcd_cs]

set_property PACKAGE_PIN T14 [get_ports lcd_rst]
set_property IOSTANDARD  LVCMOS33 [get_ports lcd_rst]

set_property PACKAGE_PIN U13 [get_ports lcd_blk]
set_property IOSTANDARD  LVCMOS33 [get_ports lcd_blk]

# J10 第28脚（EX_IO1_13P）
set_property PACKAGE_PIN V12 [get_ports ws2812_din]
set_property IOSTANDARD  LVCMOS33 [get_ports ws2812_din]

