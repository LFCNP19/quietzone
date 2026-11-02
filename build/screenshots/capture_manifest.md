# 截图清单（capture manifest）

采集时间：2026-10-31
工具：Vivado 2026.1（win64, Build 6511674）
工程：`sound_loc_integrated.xpr`，器件 xc7z020clg400-2
采集方式：真实桌面窗口截图，DPI 感知（原生 2560x1600），未重绘、未拼接。

| 文件 | 界面状态 | 进入该状态的命令 | 证明的结论 |
| --- | --- | --- | --- |
| `impl_dashboard.png` | Project Summary / Dashboard | `open_project ...; open_run impl_1` | 综合与实现均完成；WNS +1.135 ns、TNS 0；LUT 8%、BRAM 24%、DSP 3%；功耗 1.73 W；底部为层次化资源表（4187 LUT / 4614 FF / 34 BRAM / 7 DSP） |
| `sim_acq_waveform.png` | 仿真波形窗口 | `launch_simulation`（sim_1 顶层 `tb_acq_top`） | 四路通道数据、BRAM 写地址与握手计数；Console 显示 XSim 正常加载并运行 |
| `sim_acq_pass.png` | 仿真 Tcl Console | `run all` | 输出 `>>> PASS <<<`，`$finish called at time : 1964610 ns` |
| `synth_acq_ooc.png` | Synthesized Design（OOC） | `open_run system_acq_top_0_0_synth_1` | 采集通路模块级综合完成，层次化资源视图 |

原始全窗口截图保存在 `_source/`。裁剪只去掉与结论无关的区域。
