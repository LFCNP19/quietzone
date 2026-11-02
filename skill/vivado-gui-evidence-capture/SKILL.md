---
name: vivado-gui-evidence-capture
description: 在 Vivado / Vitis 的真实图形界面中采集仿真波形、综合报告、实现报告与工程状态截图，产出可复核、不可伪造的界面证据。适用于竞赛提交、设计评审和问题复盘；不适用于只需要文本报告的场合。
---

# Vivado 界面证据采集

## 1. 这个技能解决什么问题

很多工程文档里的"仿真截图""综合截图"其实是重绘的示意图，评审无法确认工具真的跑过。本技能把"打开工程 → 进入目标状态 → 等待渲染 → 窗口级截图 → 记录元数据"固化成可重复流程，保证每张图都能用同一条命令复现。

## 2. 何时使用

- 需要在报告或答辩中证明仿真确实运行并通过。
- 需要展示综合/实现的资源、时序、功耗界面，而不是只贴 `.rpt` 文本。
- 需要留下"某年某月某版本工具"的界面证据，供后续复核。

## 3. 何时不使用

- 只需要数值结论：直接引用 `.rpt`，不必截图。
- 目标机没有桌面会话：先配置虚拟显示，否则本流程不成立。
- 图像只用于版式示意：应明确标注"示意图"，不要混入证据链。

## 4. 前置条件

1. 工具已安装且能打开目标工程（本工程使用 Vivado 2026.1）。
2. 有一个真实桌面会话；截图进程必须声明 DPI 感知，否则高缩放屏幕会得到降采样图。
3. 输出目录已存在，命名遵循 `sim_*.png` / `synth_*.png` / `impl_*.png`。
4. 明确本次要证明的结论（例如"WNS > 0""96 次写入 0 错误"），据此选择界面状态。

## 5. 工作流

### 5.1 固定证据清单

先写清楚每张图要证明什么，再决定打开哪个界面。典型清单：

| 结论 | 界面状态 |
| --- | --- |
| 仿真跑通 | 波形窗口 + Tcl Console 的 `>>> PASS <<<` |
| 时序收敛 | Project Summary 的 Timing 面板，或 Timing Summary 报告页 |
| 资源占用 | Project Summary 的 Utilization 面板，或 Utilization 报告页 |
| 综合完成 | Synthesis 状态为 Complete，或模块级综合的 Utilization 报告 |

### 5.2 用脚本把软件带到目标状态

不要手工点菜单，尽量用 Tcl 脚本一次性完成，减少人为差异：

```tcl
open_project <project.xpr>
open_run impl_1
report_utilization -name gui_utilization_placed
report_timing_summary -name gui_timing_summary -max_paths 10
```

仿真则用工程内的 `sim_1` 直接启动：

```tcl
launch_simulation
run all
```

`report_*` 在 GUI 模式下会把结果放进底部报告面板，脚本执行完即可截图。

### 5.3 等待渲染完成

用"就绪文件 + 固定等待"代替盲目睡眠：脚本写完 `ready.txt` 后，截图端再等 2-3 秒让界面重绘完成。

### 5.4 窗口级截图（关键）

调用 `scripts/capture_window.ps1`，它做了三件事：

1. `SetProcessDPIAware()`，避免 150% 缩放把 2560x1600 截成 1707x1067。
2. `GetWindowRect` 只截目标窗口，不截整个桌面。
3. `SetForegroundWindow` + 最大化，保证窗口没有被遮挡。

```powershell
.\scripts\capture_window.ps1 -OutPath impl_dashboard.png -TitleMatch Vivado
```

### 5.5 裁剪与命名

- 保留窗口标题栏或菜单栏，证明截图来自真实软件。
- 裁掉与结论无关的区域，但不要拼接、不要重绘。
- 文件名体现内容：`sim_acq_waveform.png`、`impl_dashboard.png`、`synth_acq_util.png`。

### 5.6 写元数据清单

在同目录生成 `capture_manifest.md`，逐张记录：文件名、工具版本、工程路径、进入该界面的 Tcl 命令、截图时间、要证明的结论。

## 6. 已知坑（本工程实测）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 截图像素只有 1707x1067 | 截图进程未声明 DPI 感知，系统按 150% 缩放虚拟化 | 先 `SetProcessDPIAware()` |
| 粘贴的 Tcl 命令里空格变成中文 | 输入法处于中文状态 | 用剪贴板粘贴，或临时切换到英文键盘布局 |
| 报告生成了却看不到 | `report_*` 结果在底部面板的标签页里 | 截图包含底部面板，或改用 Project Summary 仪表板 |
| 报告表格太小 | 底部面板高度有限 | 优先截 Project Summary 的 Dashboard，信息密度更高 |

## 7. 完成判定

- 每张图都能用清单里的命令重新生成。
- 图中数值与对应 `.rpt` 文件一致（逐项核对，不允许"大概一致"）。
- 图中不含参考图、重绘图、后期添加的文字或箭头。
- 元数据清单完整。

## 8. 本工程的验证记录

- `sim_acq_waveform.png`：Vivado 2026.1 波形窗口，`tb_acq_top` 运行到 1964610 ns。
- `sim_acq_pass.png`：同一会话的 Tcl Console，输出 `>>> PASS <<<` 与 `$finish called at time : 1964610 ns`。
- `impl_dashboard.png`：Project Summary 仪表板，WNS +1.135 ns、LUT 8%、BRAM 24%、功耗 1.73 W。
- `synth_acq_util.png`：模块级综合 `system_acq_top_0_0_synth_1` 的 Utilization 报告。

## 9. 迁移到其它工程

只需要替换三处：工程路径、`open_run` 的运行名、`report_*` 的参数。流程、DPI 处理、命名规则和元数据清单可以直接复用。
