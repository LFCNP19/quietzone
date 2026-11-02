# src/ —— 设计源码

## rtl/（8 个可综合 Verilog 模块，一条 PL 数据通路的全部）

```
acq_top                    ← 采集域顶层，50 MHz 输入，输出 wr_cnt 给 PS
├─ i2s_rx ×4               ← 四路 INMP441，共享 SCK/WS，各自一根 SD
└─ acq_to_bram ×4          ← 每通道一个乒乓写控制器，写同一块 2048×64 共享 BRAM

lcd_led_top                ← 显示域顶层，两个 AXI GPIO 的 64 bit 控制字直接当输入
├─ lcd_ring_ui             ← 色环 + 声压大字 + 汉字状态，画进帧存
├─ lcd_framebuf            ← 240×240 RGB565 双口帧存（29 块 BRAM36）
├─ lcd_spi_master          ← 25 MHz 硬件 SPI，约 27 fps 扫屏
└─ ws2812b_drv             ← 16 灯 GRB 纳秒时序
```

| 文件 | 职责 | 关键点 |
| --- | --- | --- |
| `acq_top.v` | 采集域顶层 | 3.125 MHz SCK / 48.83 kHz WS 由 50 MHz 分频得到；汇总四路写计数 |
| `i2s_rx.v` | 单路 I2S 接收 | 24 bit 左对齐，取高 16 bit；下降沿换数、上升沿采样 |
| `acq_to_bram.v` | 打包 + 乒乓写 | FILL/WAIT 两态，写满一 bank 交还 PS，双 bank 交替 |
| `lcd_led_top.v` | 显示域顶层 | `ctrl1` = fill/state/db10，`ctrl2` = led_color/led_update |
| `lcd_framebuf.v` | 帧存 | 双口，PS 写、扫描读 |
| `lcd_spi_master.v` | 刷屏引擎 | 命令字 DC=0、像素 DC=1，每像素发 16 bit RGB565；`PIX_BITS=8` 时把 RGB332 按位扩成 RGB565（GC9A01 的 COLMOD 不支持 8 位） |
| `lcd_ring_ui.v` | UI 渲染 | 三档配色、声压比例条、状态汉字；比例运算不在此模块做乘除（见报告 §3.6） |
| `ws2812b_drv.v` | 灯环时序 | 0 码 20/42 拍、1 码 40/22 拍、帧间 RESET 300 µs |

## ps/（Zynq PS 裸机程序，Vitis 2026.1）

| 文件 | 作用 |
| --- | --- |
| `main.c` | 主循环：读帧 → A 计权声压 → 本底跟踪 → 特征 → GCC-PHAT 方位 → 状态机 → 屏/灯/遥测 |
| `state_logic.h` | 三态判定与滞回（进 WARN 4 s、进 ALARM 3 s、回差与短时静音容错） |
| `display_ctrl.h` | 圆屏/灯环控制字的组包与刷新节拍 |
| `CMakeLists.txt` / `UserConfig.cmake` / `Empty_applicationExample.cmake` | Vitis 工程配置文件 |
| `lscript.ld` | 链接脚本（DDR 布局） |
| `app.yaml` | 平台/应用元数据 |

## constr/

`sound_loc_four_mic.xdc`：14 根外部端口的 `PACKAGE_PIN` / `IOSTANDARD`，以及 50 MHz 输入时钟约束。
BD 形态下时钟由 PS `FCLK_CLK0` 提供，约束里不重复 `create_clock`。

## bd/

`create_sound_loc_project.tcl`（从零建工程到出位流）与 `run_bd_impl.tcl`（只重跑实现），
详见 `bd/README.md`。

## host/（PC 端，纯 Python 标准库）

| 文件 | 作用 |
| --- | --- |
| `stats_server.py` | 本地 HTTP 服务：聚合遥测、15 分钟档历史、REST 接口、静态页面 |
| `hw_ifc.py` | 寄存器映射与读写（串口 / JTAG 桥 / Mock 三种后端），`--selftest` 自检 |
| `reporter.py` / `cloud_server.py` | 上报客户端与接收端（断网入队、恢复补传） |
| `doa_ref.py` / `pinmap.py` | 方位解算参考实现与引脚表 |
| `gen_frame_window.py` | 生成 256 点汉宁窗（`data/golden/` 的来源） |
| `web/` | 前端页面：实时状态、四路电平、方位环、噪音日历（Chart.js 本地副本） |
