# board/ —— 上板工程与实测

| 路径 | 内容 |
| --- | --- |
| `bitstream/sound_loc_integrated.bit` | 随包位流（4 045 769 B），与 `build/reports/` 的报告同一次 `impl_1` |
| `xsa/sound_loc_integrated.xsa` | 导出给 Vitis 的平台（497 935 B），含位流 |
| `elf/sound_loc.elf` | PS 裸机程序（248 136 B），JTAG 桥按符号 `g_qz_mem_telemetry` 读遥测 |
| `run/` | 上板脚本：JTAG 桥、仪表盘启动、链路恢复 |
| `measurements/` | 实测与标称对照、现场样本结论、待补齐项 |

## 上板步骤

1. **下载位流**：Vivado → Hardware Manager → Auto Connect → Program Device，
   选 `board\bitstream\sound_loc_integrated.bit`。
2. **运行 PS 程序**（位流下载完成后 PS 还停在复位向量，不跑 ELF 就没有遥测）：

   ```powershell
   & D:\VIVA\2026.1\Vitis\bin\xsdb.bat -eval 'targets -set -filter {name =~ APU}; dow C:/qz/board/elf/sound_loc.elf; con'
   ```

   把 ELF 路径换成本机实际位置；跑起来后 `mrd 0x00109280` 应读到魔数 `0x515A544D`（`QZTM`）。
3. **起仪表盘**：

   ```powershell
   python .\board\run\start_jtag_dashboard.py
   ```

   脚本先拉起 `qz_jtag_bridge.py`（通过 Vitis 的 xsdb 访问 AXI 空间，监听 8765），
   再拉起 `src\host\stats_server.py`（监听 8088，`--source serial`）。
   浏览器打开 `http://127.0.0.1:8088/`。
4. **链路异常**（连不上 AXI / 读回全 0 / 位流加载失败）：

   ```powershell
   & D:\VIVA\2026.1\Vitis\bin\vitis.bat -s .\board\run\recover_sound_loc_jtag.py
   ```

   脚本先对 target 1/2 依次做 system reset，再 `fpga -file` 重新下载位流。

   **复位后注意**：`rst -system` 会把 PS 拉回未初始化状态，随后 PL 的 AXI 从机返回
   `Level shifters are not enabled`。这个脚本只做复位与重下位流，还需按下面补一次 PS 初始化，再回到第 2 步跑 ELF：

   ```powershell
   & D:\VIVA\2026.1\Vitis\bin\xsdb.bat -eval 'source C:/qz/ps7_init.tcl; ps7_init; ps7_post_config'
   ```

   `ps7_init.tcl` 从随包 `board\xsa\sound_loc_integrated.xsa` 解出（在 `hw/ps7_init.tcl`），不需额外维护一份。

三个脚本的路径都相对提交包推导，只有 Vivado/Vitis 安装位置需要按本机改：
`start_jtag_dashboard.py` 读 `QZ_VITIS`，`qz_jtag_bridge.py` 读 `QZ_ELF` / `QZ_ARM_NM`，
`recover_sound_loc_jtag.py` 读 `QZ_BIT`。

## 前提与边界

- 当前交付以 **JTAG 下载** 为准；QSPI/SD 冷启动未打通（见 `measurements/measured_vs_reference.md`）。
- 仪表盘只读遥测，不写 PL 寄存器；改阈值等操作走 `src\host\hw_ifc.py` 的寄存器接口。
- 圆屏与灯环的时序由 PL 产生、颜色与状态由 PS 经 AXI GPIO 下发，都跑在板内；
  网页断线只影响远程查看，不影响现场反馈。
