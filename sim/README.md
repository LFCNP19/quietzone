# sim/ —— 仿真

## 怎么跑

```powershell
powershell -ExecutionPolicy Bypass -File .\sim\run_sim.ps1
powershell -ExecutionPolicy Bypass -File .\sim\run_sim.ps1 -VivadoBin "D:\VIVA\2026.1\Vivado\bin"
```

脚本对两套 testbench 各做一遍 `xvlog → xelab → xsim -runall`，互不共享编译库：

| testbench | 编译文件 | 说明 |
| --- | --- | --- |
| `tb_acq_top.v` | `tb_acq_top.v` + `acq_top.v` + `acq_to_bram.v` + `i2s_rx.v` | 采集通路：4 路 I2S 收数 → 乒乓 BRAM → PS 侧读帧 |
| `tb_ws2812b_drv.sv` | `tb_ws2812b_drv.sv` + `ws2812b_drv.v`（`xvlog -sv`） | 灯环时序：在 `o_din` 上做游程解码，逐位还原后比对 |

`logs/` 下是原始控制台输出（`xsim` 自己写的 `xsim.log` 留在临时工作目录，不混进这里）。

## 两个 testbench 的检查方式

**`tb_acq_top`** 不读 DUT 内部寄存器，只从外部行为判定：

- 麦克风模型跟随 DUT 的 `cnt` 在 SCK 下降沿换数据、上升沿被采样，模拟真实 I2S 从机；
- 逐帧核对 BRAM 写入序列：地址必须按 bank0 (0..15) / bank1 (16..31) 交替，每帧 16 次写；
- 数据比对：写进 BRAM 的 16 bit 必须等于该通道该时刻的采样值；
- 握手计数：`sample_valid` 脉冲数与写次数一致，PS 侧读帧数与写入帧数一致。

**`tb_ws2812b_drv`** 完全不看内部信号，只解 `o_din`：

- 每位必须满足高电平 20 拍（0 码）/ 40 拍（1 码），低电平 42 / 22 拍，且位周期恒为 62 拍；
- 帧前 RESET ≥ 300 µs；帧尾低电平不小于 RESET 门限；
- GRB 顺序（首灯 `0xA1B2C3` 必须先发 `G=0xA1`）；
- 忙时 `i_update` 必须被忽略且不补发；帧间连续更新能正确发出；
- 参数化：`LED_NUM=8` 与 `LED_NUM=16` 两种配置都要成帧正确。

## 结论

| testbench | 结束时间 | 结果 |
| --- | --- | --- |
| `tb_acq_top` | 1 964 610 ns | `>>> PASS <<<`，96 次 BRAM 写、0 数据错误、0 地址序列错误 |
| `tb_ws2812b_drv` | 7 300 040 ns | `RESULT: PASS (checks=30, errors=0)` |

原始日志：`logs/tb_acq_top_xsim.log`、`logs/tb_ws2812b_drv_xsim.log`；结论汇总在 `results/`。

## 覆盖不到的地方

仿真只覆盖 PL 侧数字逻辑。PS 程序、LCD 实际显示效果、麦克风模拟前端（偏置/增益）、
以及 JTAG 链路都不在 testbench 范围内，这些由 `board/measurements/` 的实测记录兜底。
