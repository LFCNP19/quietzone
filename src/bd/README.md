# bd/ —— Block Design 与构建脚本

| 文件 | 作用 |
| --- | --- |
| `create_sound_loc_project.tcl` | 从零建工程：加 RTL / 约束 / 仿真平台 → 搭 BD → 校验 → 综合 → 实现 → 位流 → XSA → 报告 |
| `run_bd_impl.tcl` | 已有工程时只重跑综合 + 实现 + 位流（不重建 BD） |

两个脚本都只认 `QZ_BUILD_DIR`，路径相对提交包推导，所以放在哪台机器上都能跑。

## 怎么跑

```powershell
# 推荐：包一层，自动设环境变量并检查产物
powershell -ExecutionPolicy Bypass -File .\build\build.ps1

# 或者直接跑 Tcl
$env:QZ_BUILD_DIR = "D:\qz_build"
& D:\VIVA\2026.1\Vivado\bin\vivado.bat -mode batch -notrace -source .\src\bd\create_sound_loc_project.tcl
```

`create_sound_loc_project.tcl` 支持三个环境变量：

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `QZ_BUILD_DIR` | 工程与产物目录 | `<上层>/quietzone_build/sound_loc_final` |
| `SL_STAGE` | 停在哪个阶段：`bd` / `synth` / `all` | `all` |
| `QZ_JOBS` | 仅 `run_bd_impl.tcl` 使用 | `8` |

## BD 里有什么

```
processing_system7_0（PS7：DDR3 + UART1/MIO48-49 + SD0/MIO40-45 + M_AXI_GP0 + FCLK_CLK0=50 MHz）
  ├─ M_AXI_GP0 → axi_interconnect_0（1 主 3 从）
  │    ├─ M00 → axi_bram_ctrl_0/S_AXI   @ 0x4000_0000  16 KB  → blk_mem_gen_0（2048×64）
  │    ├─ M01 → axi_gpio_0/S_AXI        @ 0x4120_0000  64 KB  → 握手：wr_cnt 读 / rd_cnt 写
  │    └─ M02 → axi_gpio_1/S_AXI        @ 0x4121_0000  64 KB  → 屏与灯：fill/state/db10、led_color/led_update
  ├─ FCLK_CLK0 50 MHz → 全部 AXI 侧 + acq_top_0/clk_50m + lcd_led_top_0/clk_50m
  └─ FCLK_RESET0_N → rst_ps7_50M → 各 AXI 复位 + lcd_led_top_0/rst_n

acq_top_0      → bram_addr/din/we/en（Port B），mic_sck/ws/sd0..3 出到顶层
lcd_led_top_0  → lcd_scl/sda/dc/cs/rst/blk + ws2812_din 出到顶层
```

顶层外部端口共 14 根，与 `src/constr/sound_loc_four_mic.xdc` 一一对应，也与报告里的接口冻结清单一致。

## 跑完该看到什么

- BD 校验通过，端口/连线清单打印完整（14 个 PORT）；
- 综合、实现 `PROGRESS: 100%`，`write_bitstream` 完成；
- `<QZ_BUILD_DIR>/sound_loc_integrated.bit`、`sound_loc_integrated.xsa`；
- `<QZ_BUILD_DIR>/reports/` 下 timing / utilization / drc / clock 四份报告。

## 已知但无害的告警

| 告警 | 说明 |
| --- | --- |
| `[BD 41-1306]` 接口连线被用户网络覆盖 | 故意把 `blk_mem_gen_0` 的 Port B 与 GPIO 通道拆成独立网络，方便按名字核对，不影响网表 |
| `[IP_Flow 19-4751] bram_clk FREQ_HZ missing` | `axi_bram_ctrl` 的 `bram_clk` 是输出时钟接口，频率由 `FCLK_CLK0` 决定，综合与实现均通过 |
| `[BD 41-702] WRITE_WIDTH_B/READ_WIDTH_B 覆盖被忽略` | BMG 的位宽由 IP 参数固定，传播值被忽略属预期 |
| `[BD 41-2383] addrb 32 bit 接 11 bit` | `acq_top` 的 `bram_addr` 是 11 bit（2048 深），BMG 的 `addrb` 是 32 bit；高位本就该悬空，实际只用低 11 位寻址，仿真与上板均已验证 |
