# build/ —— 构建流程与证据快照

## 目录

| 路径 | 是什么 |
| --- | --- |
| `build.ps1` | 一键构建：Block Design → 综合 → 实现 → 位流 → XSA → 报告 |
| `reports/` | **随包位流对应的报告快照**（2026-10-28 实现，Vivado 2026.1），只读存档 |
| `screenshots/` | Vivado 界面真实截图 + `capture_manifest.md` |
| `out/` | 重新构建时生成的工程目录（约 100 MB，不进提交包） |

## 一键构建

```powershell
powershell -ExecutionPolicy Bypass -File .\build\build.ps1
powershell -ExecutionPolicy Bypass -File .\build\build.ps1 -BdOnly          # 只搭 BD，约 30 s
powershell -ExecutionPolicy Bypass -File .\build\build.ps1 -BuildDir D:\qz_build -Jobs 4
```

脚本把 `-BuildDir` 写进 `QZ_BUILD_DIR` 环境变量，再交给 `src\bd\create_sound_loc_project.tcl`；
Tcl 侧只认这个变量，所以脚本与工程目录完全解耦。构建日志落在 `$QZ_BUILD_DIR\build.log`。

全流程实测 **6.7 min**（Ryzen 9 8940HX / 32 GB，`-Jobs 8`；单线程跑同一条流程实测 15.1 min），`-BdOnly` 实测 25 s。

## 报告快照（对应 `board\bitstream\sound_loc_integrated.bit`）

| 文件 | 用途 | 关键结论 |
| --- | --- | --- |
| `system_wrapper_timing_summary_routed.rpt` | routed 时序 | WNS **+1.135 ns**、WHS **+0.055 ns**、TNS 0、失败端点 0 / 14087 |
| `system_wrapper_utilization_placed.rpt` | 布局后资源 | LUT 4187（7.87 %）、FF 4614（4.34 %）、BRAM 34（24.29 %）、DSP 7（3.18 %） |
| `system_wrapper_utilization_synth.rpt` | 综合后资源 | 与实现后一致，FPGA 侧无额外膨胀 |
| `system_wrapper_drc_routed.rpt` | routed DRC | 无 Error / 无 Critical Warning；8 类 Warning（DPIP/DPOP/RBOR/REQP 等，均为 BRAM 输出寄存器与 DSP 流水建议） |
| `system_wrapper_clock_utilization_routed.rpt` | 时钟资源 | 单时钟域 50 MHz（PS `FCLK_CLK0`） |

重跑 `build.ps1` 会在 `$QZ_BUILD_DIR\reports` 下生成同名报告，**不会覆盖**这里的存档；
评审时以随包位流 + 这里的快照为准，两者出自同一次 `impl_1`。

## 界面截图

4 张真图，均为 DPI 感知的 2560×1600 原生窗口截图，裁剪只去掉与结论无关的区域：

| 文件 | 界面 | 证明 |
| --- | --- | --- |
| `impl_dashboard.png` | Project Summary 仪表盘（`open_run impl_1` 后） | 时序 / 资源 / 功耗同一屏可见；底部层次化资源表 4187 / 4614 / 34 / 7 |
| `sim_acq_waveform.png` | 仿真波形窗口（sim_1，顶层 `tb_acq_top`） | 四通道数据、BRAM 写地址与握手计数随激励变化 |
| `sim_acq_pass.png` | 仿真 Tcl Console | `>>> PASS <<<`，`$finish` 于 1 964 610 ns |
| `synth_acq_ooc.png` | Synthesized Design（`system_acq_top_0_0_synth_1`） | 采集通路模块级综合完成，OOC 资源可见 |

逐张的界面状态 / 进入命令 / 能证明什么，见 `screenshots/capture_manifest.md`。
