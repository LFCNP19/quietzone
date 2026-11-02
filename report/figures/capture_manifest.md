# 截图清单（capture manifest）

采集时间：2026-10-31（`fig22_dashboard_live.png` 于 2026-11-01 按当前界面重拍）
工具：Vivado 2026.1（win64, Build 6511674）
工程：`sound_loc_integrated.xpr`，器件 xc7z020clg400-2
采集方式：真实桌面窗口截图，DPI 感知（原生 2560x1600），未重绘、未拼接。

| 文件 | 界面状态 | 进入该状态的命令 | 证明的结论 |
| --- | --- | --- | --- |
| `impl_dashboard.png` | Project Summary / Dashboard | `open_project ...; open_run impl_1` | 综合与实现均完成；WNS +1.135 ns、TNS 0；LUT 8%、BRAM 24%、DSP 3%；功耗 1.73 W；底部为层次化资源表（4187 LUT / 4614 FF / 34 BRAM / 7 DSP） |
| `sim_acq_waveform.png` | 仿真波形窗口 | `launch_simulation`（sim_1 顶层 `tb_acq_top`） | 四路通道数据、BRAM 写地址与握手计数；Console 显示 XSim 正常加载并运行 |
| `sim_acq_pass.png` | 仿真 Tcl Console | `run all` | 输出 `>>> PASS <<<`，`$finish called at time : 1964610 ns` |
| `synth_acq_ooc.png` | Synthesized Design（OOC） | `open_run system_acq_top_0_0_synth_1` | 采集通路模块级综合完成，层次化资源视图 |

| `fig22_dashboard_live.png` | 上位机网页（统计看板，已连接） | `vitis -s qz_jtag_bridge.py` 起 JTAG 内存遥测桥，`stats_server.py --source serial --serial-port socket://127.0.0.1:8765 --port 8088` 起网页 | 板卡在片运行：状态“安静区”、SPL 53.4 dB(A)、四麦实时电平，页面右上显示“已连接”。首屏只保留声压级与当前状态两块；方位面板指针取最新一帧解算角（此处方位角 2.2°、复核窗口 19 帧、角度离散度 31.1°、自检残差 7.7 %、阵列孔径比 0.11×，本帧自检未通过，徽标“待复核 · 刚刚”），页面以 5 Hz 轮询、指针 0.26 s 线性过渡，统计卡与图表仍按 1 Hz 刷新；面板不显示置信度百分比 |
原始全窗口截图保存在 `_source/`。裁剪只去掉与结论无关的区域。
