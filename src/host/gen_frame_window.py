#!/usr/bin/env python3
"""生成 frame_builder 用的 256x8 汉宁窗 ROM。

w[n] = 0.5 - 0.5*cos(2*pi*n/N)，量化为 8 bit（0..255）。

产物：
  rtl/frame_window_rom.v          自带初值的 ROM 模块（供 RTL 直接使用，无文件依赖）
  rtl/frame_window_256x8.coe      Vivado COE（供将来换 blk_mem_gen IP 时使用）

改窗口长度或位宽时改这里的 N/WB，然后重新运行本脚本，不要手改 rtl/frame_window_rom.v。
"""
import math
from pathlib import Path

N  = 256
WB = 8
FULL = (1 << WB) - 1          # 255

root = Path(__file__).resolve().parents[1]
rtl  = root / "rtl"
rtl.mkdir(parents=True, exist_ok=True)

vals = []
for n in range(N):
    w = 0.5 - 0.5 * math.cos(2.0 * math.pi * n / N)
    vals.append(math.floor(w * FULL + 0.5))
assert vals[0] == 0 and vals[N // 2] == FULL and vals[-1] <= 2, vals[:4]
assert min(vals) >= 0 and max(vals) <= FULL

lines = []
lines.append("//======================================================================")
lines.append("// frame_window_rom — %d 点 %d bit 汉宁窗查找表" % (N, WB))
lines.append("//")
lines.append("// 本文件由 python/gen_frame_window.py 生成，请勿手工修改。")
lines.append("// 公式：w[n] = 0.5 - 0.5*cos(2*pi*n/%d)，量化为 0..%d" % (N, FULL))
lines.append("// 组合读，调用方负责打一拍对齐流水线。")
lines.append("//======================================================================")
lines.append("`timescale 1ns / 1ps")
lines.append("")
lines.append("module frame_window_rom #(")
lines.append("    parameter integer N = %d" % N)
lines.append(")(")
lines.append("    input  wire [7:0] i_adr,")
lines.append("    output wire [7:0] o_win")
lines.append(");")
lines.append("")
lines.append("    reg [7:0] rom [0:N-1];")
lines.append("")
lines.append("    initial begin")
for n in range(N):
    lines.append("        rom[%3d] = 8'h%02X;" % (n, vals[n]))
lines.append("    end")
lines.append("")
lines.append("    assign o_win = rom[i_adr];")
lines.append("")
lines.append("endmodule")
(rtl / "frame_window_rom.v").write_text("\n".join(lines) + "\n", encoding="utf-8")

coe = ["memory_initialization_radix=16;", "memory_initialization_vector="]
for i in range(0, N, 16):
    chunk = ", ".join("%02X" % v for v in vals[i:i + 16])
    coe.append(chunk + ("," if i + 16 < N else ";"))
(rtl / "frame_window_256x8.coe").write_text("\n".join(coe) + "\n", encoding="utf-8")

print("生成完成：%d 项，min=%d max=%d，峰值在 n=%d" % (N, min(vals), max(vals), vals.index(max(vals))))
