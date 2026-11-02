# 静域 QuietZone —— 四麦克风声源定位装置（2026 AMD FPGA 竞赛提交包）

在 Zynq-7020 上用 4 路 I2S 数字麦克风做声源定位：PL 负责采集、乒乓帧存、圆屏与灯环呈现，
PS 负责状态机、打分与显示调度；50 MHz 单时钟域，全部接口引脚固定，Vivado 2026.1 综合实现通过。

| 项目 | 结果 | 出处 |
| --- | --- | --- |
| 器件 / 工具 | xc7z020clg400-2 / Vivado 2026.1（SW Build 6511674） | `build/reports/` |
| 时序（routed） | WNS **+1.135 ns**、WHS **+0.055 ns**、TNS 0、失败端点 0 / 14087 | `build/reports/system_wrapper_timing_summary_routed.rpt` |
| 资源 | LUT 4187（7.87 %）、FF 4614（4.34 %）、BRAM 34（24.29 %）、DSP 7（3.18 %） | `build/reports/system_wrapper_utilization_placed.rpt` |
| 功耗 | 片上 1.73 W，结温 45.0 ℃ | `build/screenshots/impl_dashboard.png` |
| 仿真 | `tb_acq_top` PASS（1 964 610 ns，96 次写、0 错误）；`tb_ws2812b_drv` PASS（30 项检查、0 错误） | `sim/logs/` |
| 位流 / XSA | `sound_loc_integrated.bit`（4 045 769 B）/ `sound_loc_integrated.xsa`（497 935 B） | `board/` |

## 目录结构

```
.
├─ README.md
├─ src/
│  ├─ rtl/          8 个可综合 Verilog 模块（采集 / 乒乓帧存 / 圆屏 UI / LCD SPI / WS2812B）
│  ├─ ps/           Zynq PS 裸机程序（Vitis 2026.1：main.c、状态机、显示调度、链接脚本）
│  ├─ constr/       引脚与时序约束 sound_loc_four_mic.xdc
│  ├─ bd/           一键建工程 create_sound_loc_project.tcl / 重跑实现 run_bd_impl.tcl
│  └─ host/         PC 端 Python 服务与网页仪表盘（统计、上报、JTAG 桥对接）
├─ sim/
│  ├─ tb/           tb_acq_top.v、tb_ws2812b_drv.sv（自检，不看内部信号）
│  ├─ run_sim.ps1   一键跑两个 testbench
│  ├─ logs/         真实 XSim 输出日志
│  └─ results/      仿真结论摘要
├─ build/
│  ├─ build.ps1     BD → 综合 → 实现 → 位流 → XSA 全流程
│  ├─ reports/      随包位流对应的时序 / 资源 / DRC / 时钟报告快照
│  └─ screenshots/  Vivado 软件内真实界面截图 + 采集清单
├─ board/
│  ├─ bitstream/    sound_loc_integrated.bit
│  ├─ xsa/          sound_loc_integrated.xsa
│  ├─ elf/          PS 裸机程序 sound_loc.elf
│  ├─ run/          上板脚本（JTAG 桥、仪表盘启动、链路恢复）
│  └─ measurements/ 实测记录与对照表
├─ data/
│  ├─ golden/       帧窗金标准 frame_window_256x8.coe
│  └─ samples/      现场采集样本（jsonl，含 FSM 与分类器输出）
├─ skill/            6 个可复用技能（含 Vivado 界面取证与两个 PYNQ 技能）
└─ report/           设计报告（25 页）、图表与截图清单
```

## 从零复现

### 0. 环境

- Vivado / Vitis **2026.1**（2025.2 亦可）；器件库需含 Zynq-7000。
- Python 3.10+（只跑 PC 端脚本时需要），无第三方依赖即可启动仪表盘。

### 1. 仿真（约 15 s）

```powershell
powershell -ExecutionPolicy Bypass -File .\sim\run_sim.ps1
```

脚本用 `xvlog → xelab → xsim -runall` 依次跑两套 testbench，把控制台输出写进 `sim\logs\`，
末行打印 `ALL TESTBENCHES PASS`。`-VivadoBin` 可指定 Vivado 的 bin 目录。

### 2. 构建（约 7 min）

```powershell
powershell -ExecutionPolicy Bypass -File .\build\build.ps1
```

等价于直接跑 Tcl：

```powershell
$env:QZ_BUILD_DIR = "D:\qz_build"
& D:\VIVA\2026.1\Vivado\bin\vivado.bat -mode batch -notrace -source .\src\bd\create_sound_loc_project.tcl
```

脚本从零建工程、搭 Block Design（PS7 + AXI 互联 + AXI GPIO ×2 + AXI BRAM Ctrl + acq_top / lcd_led_top）、
综合、实现、出位流、导 XSA，并把报告写到 `$QZ_BUILD_DIR\reports`。默认输出目录 `build\out`
（约 100 MB，不进提交包）。只想验证 BD 能否搭通可加 `-BdOnly`（约 30 s）。

### 3. 上板

1. Vivado Hardware Manager 打开，Auto Connect，Program Device 选 `board\bitstream\sound_loc_integrated.bit`。
2. 起 PC 端仪表盘（默认 8080 端口）：

   ```powershell
   python .\board\run\start_jtag_dashboard.py
   ```

   该脚本调用 `qz_jtag_bridge.py` 通过 JTAG 读写 AXI 寄存器；链路异常时先跑
   `recover_sound_loc_jtag.py`。
3. PS 程序：Vitis 2026.1 以 `board\xsa\sound_loc_integrated.xsa` 建平台，导入 `src\ps\` 下的
   `main.c`、`state_logic.h`、`display_ctrl.h`、`CMakeLists.txt`、`lscript.ld` 与两个
   `*.cmake` 配置后编译下载。

## 数据与证据

- `build/screenshots/capture_manifest.md` 逐张说明截图对应的界面状态、进入该状态的 Tcl 命令、
  以及这张图能证明什么；全部为真实窗口截图（DPI 感知的 2560×1600 原生截图，未重绘、未拼接）。
- `sim/logs/*.log` 是 XSim 原始输出，不是整理后的摘要；`sim/results/` 只做结论汇总。
- `data/golden/frame_window_256x8.coe` 是帧窗金标准，`data/samples/*.jsonl` 是现场采集样本，
  `board/measurements/` 给出实测与标称的对照以及尚未验证的项目。

## 技能

`skill/` 下 6 个技能均带 `SKILL.md`，可独立复用：

| 技能 | 解决什么 |
| --- | --- |
| `competition-rules-intake` | 把竞赛通知/评审细则转成可核对的结构化要求清单 |
| `evidence-driven-engineering-report` | 只用可复现的证据写工程报告（数字必须能追到报告/日志/截图） |
| `submission-package-audit` | 交付前按目录、命名、证据链逐条自检 |
| `vivado-gui-evidence-capture` | 在 Vivado GUI 里定位到能证明结论的界面并截真图（含 DPI 感知脚本） |
| `pynq-overlay-bringup` | 在 PYNQ 上加载校验 Overlay，把 XSA/HWH 变成可执行的寄存器访问与冒烟测试 |
| `pynq-dma-golden-verify` | DMA 搬运与缓存一致性封装，黄金比对加吞吐与资源报告 |

详细用法与失效条件见 `skill/README.md`。

## 说明

- 顶层目录固定为 `src / sim / build / board / data / skill / report`，文件名一律小写英文。
- 硬件接口自冻结后未改动：`clk_50m, mic_sck, mic_ws, mic_sd0..3, ws2812_din, lcd_scl, lcd_sda,
  lcd_dc, lcd_cs, lcd_rst, lcd_blk, pir_out, led4, key4`。
- 设计报告见 `report/静域QuietZone技术报告.docx`；结构与赛事要求的对应表见 `report/README.md`。

## 开源与许可

- 许可证：MIT，见 `LICENSE`。硬件描述（RTL、Block Design Tcl、XDC）、PS 裸机程序、
  PC 端服务与网页、`skill/` 技能包按同一许可证发布。
- 仓库内含位流、XSA、ELF 与设计报告，便于原样复现；二次开发请自行确认麦克风阵列与
  屏/灯结构的机械与电气安全。
- 引用或派生使用时，请注明项目名与仓库地址。
