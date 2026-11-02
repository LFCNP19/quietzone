# 仿真结论汇总（2026-10-31，Vivado 2026.1 XSim）

两套 testbench 全绿。下表每个数字都能在 `sim\logs\` 的原始日志里 `Select-String` 到，
不是人工整理的估算值。

| testbench | 检查项 | 实测 | 判据 | 日志 |
| --- | --- | --- | --- | --- |
| `tb_acq_top` | BRAM 写次数 | 96 | 6 帧 × 16 次/帧 | `logs/tb_acq_top_xsim.log` |
| `tb_acq_top` | 数据错误 | 0 | 逐写入值比对 | 同上 |
| `tb_acq_top` | 地址序列错误 | 0 | bank0 0..15 / bank1 16..31 交替 | 同上 |
| `tb_acq_top` | `sample_valid` 脉冲 | 96 | 与写次数一致 | 同上 |
| `tb_acq_top` | PS 侧读帧数 | 6 | 与写入帧数一致 | 同上 |
| `tb_acq_top` | 收尾 | `>>> PASS <<<` @ 1 964 610 ns | 自检全通过才打印 | 同上 |
| `tb_ws2812b_drv` | 位宽精度 | 高 20/40 拍、低 42/22 拍、周期 62 拍 | 384 位逐位 | `logs/tb_ws2812b_drv_xsim.log` |
| `tb_ws2812b_drv` | 帧前 RESET | 300 040 ns | ≥ 300 µs | 同上 |
| `tb_ws2812b_drv` | 帧尾低电平 | 15 024 拍 | ≥ RESET 门限 | 同上 |
| `tb_ws2812b_drv` | 参数化 8 灯 | 帧尾 15 024 拍、成帧正确 | `LED_NUM=8` 也要过 | 同上 |
| `tb_ws2812b_drv` | 收尾 | `RESULT: PASS (checks=30, errors=0)` @ 7 300 040 ns | 30 项检查 0 错误 | 同上 |

## 复核命令

```powershell
Select-String -Path .\sim\logs\tb_acq_top_xsim.log      -Pattern 'BRAM writes total|data errors|address sequence errs|>>> PASS'
Select-String -Path .\sim\logs\tb_ws2812b_drv_xsim.log  -Pattern 'RESULT:|RESET|='
```

## 软件内截图

波形窗口与 Console 的 `>>> PASS <<<` 两处界面证据见 `build\screenshots\sim_acq_waveform.png`
与 `build\screenshots\sim_acq_pass.png`（在 Vivado GUI 里 `launch_simulation` + `run all` 得到，
采集方式与界面状态记录在 `build\screenshots\capture_manifest.md`）。
