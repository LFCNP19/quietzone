#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hw_ifc.py - 静域 QuietZone PS 侧寄存器访问层（方案书 §4.11 + §4.10）

作用
    PS 上跑的 Python 应用（采集调度、屏幕 UI、网页上报）都通过这一层访问 PL。
    本文件只做两件事：
      1) 把 /dev/mem 的 0x4000_0000 窗口 mmap 进来，给出 read32 / write32；
      2) 在它之上按 §4.11 的地址表做一层"寄存器名映射表 + 语义读写"。

地址窗口（BD 里 assign_bd_address 分配，见 bd/build_bd.tcl）
    0x4000_0000 + 0x00000  音频寄存器堆 reg_bank（64 KB）  -> RegBank
    0x4001_0000              （空档 64 KB：128 KB 的窗口必须 128 KB 对齐）
    0x4002_0000 + 0x00000  圆屏帧存 lcd_framebuf（128 KB） -> Framebuf
    一次 mmap 256 KB 把两块都盖住，两块各自偏移独立，互不影响。

为什么全都要能回读
    §4.11 把 TH_* 标成"只写"，但方案书自己要求"写寄存器要回读验证"。
    reg_bank 的实现口径是：**所有可写寄存器都可回读，回读值 = 当前生效值**。
    所以本层的 set_xxx() 默认都走"写 -> 回读 -> 比对"，对不上就抛 VerifyError。

总线错误怎么认
    reg_bank 对未映射地址返回 SLVERR（不是默默返回 0）。Zynq 把这个错误响应
    变成 CPU 的 data abort，Linux 再把它变成进程的 SIGBUS —— **Python 的
    try/except 抓不住它**（信号发生在 C 层，longjmp 回不来）。
    所以：
      · 正常读写（本表里的地址）直接走 mmap，零额外开销；
      · 只有"这个地址到底在不在"这种探测才走 probe()，它在 fork 出的子进程里
        读，子进程被 SIGBUS 打死也不影响主进程，返回值就是有没有人应答。

用法
    # 板子上（需要 root：/dev/mem）
    sudo python3 hw_ifc.py --dump
    sudo python3 hw_ifc.py --selftest
    sudo python3 hw_ifc.py --set spl_warn=580 --set conf_th=65
    sudo python3 hw_ifc.py --led 00FF00            # 16 颗灯全绿
    sudo python3 hw_ifc.py --fb-pattern           # 往帧存画标准测试卡
    sudo python3 hw_ifc.py --fb-check             # 回读帧存，验证 AXI BRAM 通路
    sudo python3 hw_ifc.py --watch 10             # 每秒打印一次遥测

    # 没板子也能验代码：--mock 用内置的寄存器模型（只验本文件的编码/流程）
    python3 hw_ifc.py --mock --selftest

依赖：只用标准库（os / mmap / struct / argparse / time）。
"""

from __future__ import annotations

import argparse
import mmap
import os
import struct
import sys
import time

# ----------------------------------------------------------------------
# 地址窗口
# ----------------------------------------------------------------------
REG_BASE = 0x4000_0000        # reg_bank 基址（BD 分配，与 bd/build_bd.tcl 一致）
FB_BASE = 0x4002_0000         # lcd_framebuf 经 AXI BRAM Controller 的基址
                              #   128 KB 窗口必须 128 KB 对齐，0x4001_0000 差 64 KB，
                              #   所以中间留一格空档（0x4001_0000-0x4001_FFFF 不用）
WINDOW_SIZE = 0x4_0000        # 256 KB：盖住上面两块
FB_OFF = FB_BASE - REG_BASE   # 帧存在本窗口内的偏移 = 0x20000

REG_WINDOW = 0x1_0000         # reg_bank 窗口 64 KB
FB_WINDOW = 0x2_0000          # 帧存窗口 128 KB（32768 字 x 4 B）

FB_WORDS = (240 * 240 + 1) // 2      # RGB565：每字 2 像素 -> 28800 字
FB_BYTES = 240 * 240 * 2             # 115200 B

# ----------------------------------------------------------------------
# §4.11 寄存器名映射表
#   偏移相对 REG_BASE；名字是本层对外的唯一叫法，应用层不要写裸偏移。
# ----------------------------------------------------------------------
REG_OFF = {
    # ---- 控制与状态 ----
    "CTRL":          0x00,
    "STATUS":        0x04,
    # ---- 只读遥测 ----
    "SPL_NOW":       0x08,
    "CONF_NOW":      0x0C,
    "SPL_AVG_10S":   0x10,
    "SPL_PEAK_10S":  0x14,
    "SPL_AVG_60S":   0x18,
    "SPEECH_RATIO":  0x1C,
    "DISTURB_CNT":   0x20,
    "OCCUPY_SEC":    0x24,
    "QUIET_INDEX":   0x28,
    "STATE_DWELL":   0x2C,
    "EVENT_FIFO":    0x30,
    "EV_DROP_CNT":   0x34,
    # ---- 阈值区 ----
    "TH0":           0x40,
    "TH1":           0x44,
    "TH2":           0x48,
    "TH3":           0x4C,
    "TH4":           0x50,
    "TH5":           0x54,
    "TH6":           0x58,
    "TH7":           0x5C,
    # ---- 校准 ----
    "CAL_OFFSET":    0x60,
    "CAL_GAIN":      0x64,
    # ---- 灯环 ----
    "LED_COLOR_0":   0x80,
    "LED_UPDATE":    0xC0,
    # ---- 四麦遥测（v1.1 阶段 A，只读）----
    #   0xC4 [1:0] 最强声道序号 / [2] 主声道优势有效
    "MIC_DOM":       0xC4,
    "MIC_RMS1":      0xC8,
    "MIC_RMS2":      0xCC,
    "MIC_RMS3":      0xD0,
    "MIC_RMS4":      0xD4,
    "MIC_PK1":       0xD8,
    "MIC_PK2":       0xDC,
    "MIC_PK3":       0xE0,
    "MIC_PK4":       0xE4,
    # ---- DOA 遥测（v1.1 阶段 C，只读）----
    #   0xE8 [8:0] 方位角 0..359 度 / [9] 本帧有效 / [10] 新鲜（读后清零）
    #   0xEC/0xF0 X/Y 轴时延差 Q8.8 有符号（正 = 前一个麦滞后后一个）
    #   0xF4 [15:0] X 轴互相关峰幅度 / [31:16] Y 轴
    #   0xF8 自上次读取以来的解算次数（读后清零，饱和 0xFFFF）
    "DOA_ANGLE":     0xE8,
    "DOA_DELAY_X":   0xEC,
    "DOA_DELAY_Y":   0xF0,
    "DOA_PEAK":      0xF4,
    "DOA_CNT":       0xF8,
}
LED_NUM = 16
LED_BASE = REG_OFF["LED_COLOR_0"]                      # 0x80
LED_STRIDE = 4
LED_END = LED_BASE + LED_NUM * LED_STRIDE              # 0xC0（不含）

# CTRL 位（§4.11）
CTRL_SOFT_RESET = 1 << 0      # 自清脉冲
CTRL_STAT_CLEAR = 1 << 1      # 自清脉冲
CTRL_STAT_PAUSE = 1 << 2      # 电平保持

# STATUS 位（§4.11；位定义见 docs/design_deviations.md 第 13 条）
STATUS_BUSY_FFT = 1 << 0
STATUS_BUSY_LCD = 1 << 1
STATUS_BUSY_LED = 1 << 2
STATUS_STATE_SHIFT = 3        # [4:3] 当前状态
STATUS_STATE_MASK = 0x3
STATUS_EV_OVF = 1 << 5        # EVENT_FIFO 溢出粘滞标志

STATE_NAMES = {0: "IDLE", 1: "QUIET", 2: "WARN", 3: "ALARM"}

# EVENT_FIFO 读出格式：{16'd0, 8'hA5, level_after[3:0], head[3:0]}
EV_MARKER = 0xA5
EV_MARKER_SHIFT = 8
EV_CODE_MASK = 0xF
EV_LEVEL_SHIFT = 4
EV_NAMES = {
    0: "none",
    1: "occupy_on",
    2: "occupy_off",
    3: "to_warn",
    4: "to_alarm_burst",
    5: "to_alarm_speech_15s",
    6: "back_quiet",
    7: "back_warn",
    8: "alarm_ongoing",
}

# ----------------------------------------------------------------------
# 阈值区：每个 32 位字装两个 16 位阈值（低位字段在前）
#   (字偏移, 低位域名, 高位域名, 低位出厂值, 高位出厂值)
#   出厂值 = 方案书 §5.3 thresholds.json；reg_bank 复位后就是这些值。
#
#   注意 t_back 两个字段：reg_bank 把 [15:0] 送给 state_fsm 的 i_t_back
#   （WARN -> QUIET，8 s），[31:16] 送给 i_t_back_alarm（ALARM -> WARN，20 s），
#   与方案书 §4.11 的打包表完全一致。本层按"PL 实际拿它做什么"命名：
#     t_back_from_warn  = 离开 WARN 的时间 = WARN -> QUIET = 8 s  （0x50[15:0]）
#     t_back_from_alarm = 离开 ALARM 的时间 = ALARM -> WARN = 20 s（0x50[31:16]）
#   方案书 §5.3 的 JSON 键名 t_back_warn / t_back_quiet 名字容易误读（t_back_quiet
#   其实不是"回 QUIET"），所以本层不用那两个键名，改用不会读错的 from_warn / from_alarm。
# ----------------------------------------------------------------------
TH_FIELDS = [
    (0x40, "spl_quiet",          "spl_warn",          450,  550),
    (0x44, "spl_alarm",          None,                650,  None),
    (0x48, "conf_th",            "conf_hyst",          60,   10),
    (0x4C, "t_warn",             "t_alarm",             3,   15),
    (0x50, "t_back_from_warn",   "t_back_from_alarm",   8,   20),
    (0x54, "th_flat",            "th_flux",           400,  150),
    (0x58, "th_low",             "hyst_spl_warn",     600,   30),
    (0x5C, "hyst_spl_alarm",     None,                 40,  None),
]
TH_NAMES = [n for _o, lo, hi, _a, _b in TH_FIELDS for n in ((lo, hi) if hi else (lo,))]

# §5.3 thresholds.json 的出厂值（--check-defaults 用它比对 PL 回读值）
FACTORY_TH = {
    "spl_quiet": 450, "spl_warn": 550, "spl_alarm": 650,
    "conf_th": 60, "conf_hyst": 10,
    "t_warn": 3, "t_alarm": 15,
    "t_back_from_warn": 8, "t_back_from_alarm": 20,  # 语义名；见上面的命名说明
    "th_flat": 400, "th_flux": 150, "th_low": 600,
    "hyst_spl_warn": 30, "hyst_spl_alarm": 40,
}
FACTORY_CAL_OFFSET = 0        # dB(A)x10，16 位有符号
FACTORY_CAL_GAIN = 256        # q8.8，256 = x1.0


class BusError(RuntimeError):
    """地址没有被 PL 应答（AXI SLVERR -> CPU data abort -> SIGBUS）。"""


class VerifyError(RuntimeError):
    """写进去的值回读对不上，说明通路或地址有问题。"""


# ======================================================================
# 后端 1：真硬件（/dev/mem + mmap）
# ======================================================================
class MmapBackend:
    """把物理地址 phys_base 起的 size 字节映射到用户态。需要 root。"""

    def __init__(self, phys_base=REG_BASE, size=WINDOW_SIZE, dev="/dev/mem"):
        if size & 0xFFF:
            raise ValueError("size 必须是 4 KB 的整数倍：0x%X" % size)
        self.base = phys_base
        self.size = size
        self.dev = dev
        self._fd = None
        self._mm = None
        self._open()

    def _open(self):
        if not os.path.exists(self.dev):
            raise BusError("找不到 %s -- 这一层要在 Zynq 的 Linux 上以 root 跑；"
                           "在 PC 上验代码请加 --mock" % self.dev)
        try:
            self._fd = os.open(self.dev, os.O_RDWR | os.O_SYNC)
        except PermissionError:
            raise BusError("打开 %s 需要 root：请用 sudo 运行" % self.dev)
        self._mm = mmap.mmap(self._fd, self.size, flags=mmap.MAP_SHARED,
                             prot=mmap.PROT_READ | mmap.PROT_WRITE,
                             offset=self.base)

    # ---- 基本访问 ----
    def read32(self, off):
        if off < 0 or off + 4 > self.size:
            raise BusError("偏移 0x%X 超出映射窗口 0x%X" % (off, self.size))
        return struct.unpack_from("<I", self._mm, off)[0]

    def write32(self, off, val):
        if off < 0 or off + 4 > self.size:
            raise BusError("偏移 0x%X 超出映射窗口 0x%X" % (off, self.size))
        struct.pack_into("<I", self._mm, off, val & 0xFFFF_FFFF)

    def read(self, off, n):
        return bytes(self._mm[off:off + n])

    def write(self, off, data):
        self._mm[off:off + len(data)] = data

    # ---- 探测：在子进程里读，SIGBUS 打不到主进程 ----
    def probe(self, off):
        pid = os.fork()
        if pid == 0:                       # 子进程
            try:
                self.read32(off)
            except BaseException:
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def name(self):
        return "mmap %s @0x%08X (+0x%X)" % (self.dev, self.base, self.size)

# ======================================================================
# 后端 2：PC 侧自检模型（--mock）
#   只实现"本文件用得到的语义"，用来在没有板子时验证编码/流程没写错。
#   它**不是** PL 的仿真器：位域与默认值按 §4.11 抄，行为只覆盖正常路径。
# ======================================================================
class MockBackend:
    def __init__(self, phys_base=REG_BASE, size=WINDOW_SIZE):
        self.base = phys_base
        self.size = size
        self.reg = bytearray(REG_WINDOW)
        self.fb = bytearray(FB_WINDOW)
        self.fifo = []
        self.ev_drop = 0
        self.pause = 0
        struct.pack_into("<I", self.reg, REG_OFF["CTRL"], 0)
        for off, _lo, hi, dlo, dhi in TH_FIELDS:
            struct.pack_into("<I", self.reg, off, ((dhi << 16) | dlo) if hi else dlo)
        struct.pack_into("<I", self.reg, REG_OFF["CAL_OFFSET"], FACTORY_CAL_OFFSET & 0xFFFF)
        struct.pack_into("<I", self.reg, REG_OFF["CAL_GAIN"], FACTORY_CAL_GAIN)
        struct.pack_into("<I", self.reg, REG_OFF["SPL_NOW"], 423)
        struct.pack_into("<I", self.reg, REG_OFF["CONF_NOW"], 71)
        struct.pack_into("<I", self.reg, REG_OFF["SPL_AVG_10S"], 418)
        struct.pack_into("<I", self.reg, REG_OFF["SPL_PEAK_10S"], 502)
        struct.pack_into("<I", self.reg, REG_OFF["SPL_AVG_60S"], 411)
        struct.pack_into("<I", self.reg, REG_OFF["SPEECH_RATIO"], 55)
        struct.pack_into("<I", self.reg, REG_OFF["DISTURB_CNT"], 3)
        struct.pack_into("<I", self.reg, REG_OFF["OCCUPY_SEC"], 1234)
        struct.pack_into("<I", self.reg, REG_OFF["QUIET_INDEX"], 76)
        struct.pack_into("<I", self.reg, REG_OFF["STATE_DWELL"], 42)
        struct.pack_into("<I", self.reg, REG_OFF["STATUS"], 1 << STATUS_STATE_SHIFT)
        # 四麦 / DOA 遥测区（0xC4–0xF8，050 轮接入）：给一组自洽的示例值，
        # 让 --selftest / --dump 在没有板子时也能把这一段的读通路走通。
        struct.pack_into("<I", self.reg, REG_OFF["MIC_DOM"], 0x1)      # 麦2 最强，优势位 0
        for _i, _v in enumerate((768, 1024, 512, 256)):
            struct.pack_into("<I", self.reg, REG_OFF["MIC_RMS1"] + 4 * _i, _v)
        for _i, _v in enumerate((900, 1100, 700, 500)):
            struct.pack_into("<I", self.reg, REG_OFF["MIC_PK1"] + 4 * _i, _v)
        struct.pack_into("<I", self.reg, REG_OFF["DOA_ANGLE"], (45 & 0x1FF) | (1 << 9) | (1 << 10))
        struct.pack_into("<I", self.reg, REG_OFF["DOA_DELAY_X"], (3 * 256) & 0xFFFF)
        struct.pack_into("<I", self.reg, REG_OFF["DOA_DELAY_Y"], (-2 * 256) & 0xFFFF)
        struct.pack_into("<I", self.reg, REG_OFF["DOA_PEAK"], (1200 << 16) | 1500)
        struct.pack_into("<I", self.reg, REG_OFF["DOA_CNT"], 5)

    def _chk(self, off, n):
        if off < 0 or off + n > self.size:
            raise BusError("偏移 0x%X 超出窗口" % off)

    def _mapped(self, off):
        if off + 4 > REG_WINDOW:
            return False
        if off <= REG_OFF["EV_DROP_CNT"]:
            return True
        if REG_OFF["TH0"] <= off <= REG_OFF["TH7"]:
            return True
        # 四麦 / DOA 只读遥测区（0xC4–0xF8，050 轮接入）
        if REG_OFF["MIC_DOM"] <= off <= REG_OFF["DOA_CNT"]:
            return True
        if off in (REG_OFF["CAL_OFFSET"], REG_OFF["CAL_GAIN"], REG_OFF["LED_UPDATE"]):
            return True
        return LED_BASE <= off < LED_END

    def read32(self, off):
        self._chk(off, 4)
        if FB_OFF <= off < FB_OFF + FB_WINDOW:
            return struct.unpack_from("<I", self.fb, off - FB_OFF)[0]
        if not self._mapped(off):
            raise BusError("0x%X 未映射" % off)
        if off == REG_OFF["EVENT_FIFO"]:
            if not self.fifo:
                return 0
            code = self.fifo.pop(0)
            return ((EV_MARKER << EV_MARKER_SHIFT)
                    | (len(self.fifo) << EV_LEVEL_SHIFT) | code)
        if off == REG_OFF["EV_DROP_CNT"]:
            return self.ev_drop
        if off == REG_OFF["LED_UPDATE"]:
            return 0
        if off == REG_OFF["CTRL"]:
            return self.pause << 2
        # 读后清零的两个寄存器：DOA_CNT 整字清零，DOA_ANGLE 只清 bit10（新鲜位）
        if off == REG_OFF["DOA_CNT"]:
            v = struct.unpack_from("<I", self.reg, off)[0]
            struct.pack_into("<I", self.reg, off, 0)
            return v
        if off == REG_OFF["DOA_ANGLE"]:
            v = struct.unpack_from("<I", self.reg, off)[0]
            struct.pack_into("<I", self.reg, off, v & ~(1 << 10))
            return v
        return struct.unpack_from("<I", self.reg, off)[0]

    def write32(self, off, val):
        self._chk(off, 4)
        if FB_OFF <= off < FB_OFF + FB_WINDOW:
            struct.pack_into("<I", self.fb, off - FB_OFF, val)
            return
        if not self._mapped(off):
            raise BusError("0x%X 未映射" % off)
        if off == REG_OFF["CTRL"]:
            self.pause = (val >> 2) & 1
            if val & 1:
                self.fifo = []
                self.ev_drop = 0
            return
        if off in (REG_OFF["EVENT_FIFO"], REG_OFF["EV_DROP_CNT"], REG_OFF["LED_UPDATE"]):
            return
        # 与 reg_bank 的写语义对齐：
        #   TH1 / TH7 的高 16 位是保留字段，写入后被强制归零；
        #   CAL_OFFSET / CAL_GAIN 是 16 位量，只保留低 16 位；
        #   其余阈值字（TH0/2/3/4/5/6）整字保存，高低两个阈值各占一半。
        if off in (REG_OFF["CAL_OFFSET"], REG_OFF["CAL_GAIN"],
                   REG_OFF["TH1"], REG_OFF["TH7"]):
            val &= 0xFFFF
        elif LED_BASE <= off < LED_END:
            val &= 0xFF_FFFF
        struct.pack_into("<I", self.reg, off, val)

    def read(self, off, n):
        self._chk(off, n)
        buf = bytearray(n)
        for k in range(0, n, 4):
            struct.pack_into("<I", buf, k, self.read32(off + k))
        return bytes(buf)

    def write(self, off, data):
        self._chk(off, len(data))
        pad = bytes(data) + b"\x00" * ((-len(data)) % 4)
        for k in range(0, len(data), 4):
            self.write32(off + k, struct.unpack_from("<I", pad, k)[0])

    def probe(self, off):
        try:
            self.read32(off)
            return True
        except BusError:
            return False

    def close(self):
        pass

    def name(self):
        return "mock（PC 侧自检模型，无硬件）"

# ======================================================================
# 音频寄存器堆（§4.11）
# ======================================================================
def _s16(v):
    """16 位补码 -> Python 有符号整数（Q8.8 时延用）。"""
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _q8_8_samples(v):
    """Q8.8 定点 -> 采样点（浮点），口径与 doa_engine 的 1 采样点 = 256 一致。"""
    return _s16(v) / 256.0

class RegBank:
    def __init__(self, backend, base_off=0, verify=True):
        self.b = backend
        self.base = base_off
        self.verify = verify

    # ---- 低层 ----
    def rd(self, off):
        return self.b.read32(self.base + off)

    def wr(self, off, val):
        self.b.write32(self.base + off, val)

    def rd_name(self, name):
        return self.rd(REG_OFF[name])

    def wr_checked(self, off, val):
        self.wr(off, val)
        back = self.rd(off)
        if self.verify and back != (val & 0xFFFF_FFFF):
            raise VerifyError("0x%02X 写 0x%08X 回读 0x%08X" % (off, val, back))
        return back

    # ---- 控制 ----
    def soft_reset(self):
        """CTRL.bit0：软复位 PL 声学链（自清脉冲，1 拍）。"""
        self.wr(REG_OFF["CTRL"], CTRL_SOFT_RESET)

    def stat_clear(self):
        """CTRL.bit1：统计清零（当日计数 + 事件 FIFO 溢出标志）。"""
        self.wr(REG_OFF["CTRL"], CTRL_STAT_CLEAR)

    def set_pause(self, on):
        """CTRL.bit2：统计暂停（电平保持，回读得到当前值）。"""
        self.wr(REG_OFF["CTRL"], CTRL_STAT_PAUSE if on else 0)
        got = self.pause()
        if self.verify and got != bool(on):
            raise VerifyError("统计暂停位回读不符：写 %s 回读 %s" % (on, got))
        return got

    def pause(self):
        return bool(self.rd_name("CTRL") & CTRL_STAT_PAUSE)

    # ---- 状态 ----
    def status(self):
        v = self.rd_name("STATUS")
        st = (v >> STATUS_STATE_SHIFT) & STATUS_STATE_MASK
        return {
            "raw": v,
            "fft_busy": bool(v & STATUS_BUSY_FFT),
            "lcd_busy": bool(v & STATUS_BUSY_LCD),
            "led_busy": bool(v & STATUS_BUSY_LED),
            "state": st,
            "state_name": STATE_NAMES[st],
            "ev_overflow": bool(v & STATUS_EV_OVF),
        }

    # ---- 遥测（原始值 + 换算值）----
    def snapshot(self):
        s = self.status()
        spl_now = self.rd_name("SPL_NOW") & 0xFFFF
        # 读后清零的两个地址只读一次，避免同一轮里重复取走
        doa_raw = self.rd_name("DOA_ANGLE")
        doa_dx  = self.rd_name("DOA_DELAY_X") & 0xFFFF
        doa_dy  = self.rd_name("DOA_DELAY_Y") & 0xFFFF
        doa_pk  = self.rd_name("DOA_PEAK")
        doa_cnt = self.rd_name("DOA_CNT") & 0xFFFF
        return {
            "state": s["state"],
            "state_name": s["state_name"],
            "fft_busy": s["fft_busy"],
            "lcd_busy": s["lcd_busy"],
            "led_busy": s["led_busy"],
            "ev_overflow": s["ev_overflow"],
            "spl_now_db10": spl_now,
            "spl_now_db": spl_now / 10.0,
            "conf_now": self.rd_name("CONF_NOW") & 0xFF,
            "spl_avg_10s_db10": self.rd_name("SPL_AVG_10S") & 0xFFFF,
            "spl_peak_10s_db10": self.rd_name("SPL_PEAK_10S") & 0xFFFF,
            "spl_avg_60s_db10": self.rd_name("SPL_AVG_60S") & 0xFFFF,
            "speech_ratio": self.rd_name("SPEECH_RATIO") & 0xFF,
            "disturb_cnt": self.rd_name("DISTURB_CNT") & 0xFFFF,
            "occupy_sec": self.rd_name("OCCUPY_SEC") & 0xFFFF,
            "quiet_index": self.rd_name("QUIET_INDEX") & 0xFF,
            "state_dwell_sec": self.rd_name("STATE_DWELL") & 0xFFFF,
            "ev_drop_cnt": self.rd_name("EV_DROP_CNT") & 0xFFFF,

            # --- 四麦遥测（v1.1 阶段 A）：0xC4-0xE4 ---
            #     四路窗能量/峰值每 256 个样点（约 5.24 ms）更新一次，是慢变量，
            #     直接组合读出即可；不像 DOA 那样必须同批快照。
            "mic_dom_idx": self.rd_name("MIC_DOM") & 0x3,
            "mic_dom_flag": bool((self.rd_name("MIC_DOM") >> 2) & 0x1),
            "mic_rms": [self.rd_name("MIC_RMS%d" % i) & 0xFFFF for i in (1, 2, 3, 4)],
            "mic_peak": [self.rd_name("MIC_PK%d" % i) & 0xFFFF for i in (1, 2, 3, 4)],

            # --- DOA 遥测（v1.1 阶段 C）：0xE8-0xF8 ---
            #     注意两个"读后清零"：DOA_ANGLE.bit10 新鲜位与 DOA_CNT 计数。
            #     本函数是**轮询**用途，每轮读一次正好把两者取走，语义正确；
            #     但调用方不能在一个轮询周期里重复读这两个地址。
            "doa_raw": doa_raw,
            "doa_angle_deg": doa_raw & 0x1FF,
            "doa_ok": bool((doa_raw >> 9) & 0x1),
            "doa_fresh": bool((doa_raw >> 10) & 0x1),
            "doa_delay_x_raw": doa_dx,
            "doa_delay_y_raw": doa_dy,
            "doa_delay_x": _q8_8_samples(doa_dx),
            "doa_delay_y": _q8_8_samples(doa_dy),
            "doa_peak_x": doa_pk & 0xFFFF,
            "doa_peak_y": (doa_pk >> 16) & 0xFFFF,
            "doa_solve_cnt": doa_cnt & 0xFFFF,
        }

    def next_event(self):
        """读一个事件；FIFO 空返回 None，否则返回 dict（读一次出一次队）。"""
        v = self.rd_name("EVENT_FIFO")
        if ((v >> EV_MARKER_SHIFT) & 0xFF) != EV_MARKER:
            return None
        code = v & EV_CODE_MASK
        lvl = (v >> EV_LEVEL_SHIFT) & EV_CODE_MASK
        return {
            "raw": v,
            "code": code,
            "name": EV_NAMES.get(code, "unknown_%d" % code),
            "level_after": lvl,
            "level_after_name": STATE_NAMES.get(lvl, "?"),
        }

    def drain_events(self, limit=32):
        out = []
        while len(out) < limit:
            ev = self.next_event()
            if ev is None:
                break
            out.append(ev)
        return out

    # ---- 阈值 ----
    def get_thresholds(self):
        th = {}
        for off, lo, hi, _dlo, _dhi in TH_FIELDS:
            v = self.rd(off)
            th[lo] = v & 0xFFFF
            if hi:
                th[hi] = (v >> 16) & 0xFFFF
        return th

    def th_word(self, name):
        for off, lo, hi, _dlo, _dhi in TH_FIELDS:
            if name == lo:
                return off, True
            if hi and name == hi:
                return off, False
        raise KeyError("没有这个阈值：%s（可用：%s）" % (name, ", ".join(TH_NAMES)))

    def th_names(self):
        return list(TH_NAMES)

    def set_threshold(self, name, value):
        off, is_low = self.th_word(name)
        if not 0 <= value <= 0xFFFF:
            raise ValueError("%s = %d 超出 16 位无符号范围" % (name, value))
        cur = self.rd(off)
        new = (cur & 0xFFFF_0000) | value if is_low else (cur & 0xFFFF) | (value << 16)
        back = self.wr_checked(off, new)
        got = back & 0xFFFF if is_low else (back >> 16) & 0xFFFF
        if self.verify and got != value:
            raise VerifyError("%s 写 %d 回读 %d" % (name, value, got))
        return got

    def set_thresholds(self, **kw):
        for k, v in kw.items():
            self.set_threshold(k, int(v))
        return self.get_thresholds()

    # ---- 校准 ----
    def cal_offset(self):
        v = self.rd(REG_OFF["CAL_OFFSET"]) & 0xFFFF
        return v - 0x1_0000 if v & 0x8000 else v      # 16 位有符号

    def set_cal_offset(self, v):
        if not -0x8000 <= v <= 0x7FFF:
            raise ValueError("CAL_OFFSET 是 16 位有符号数（dB x10）")
        self.wr_checked(REG_OFF["CAL_OFFSET"], v & 0xFFFF)
        got = self.cal_offset()
        if self.verify and got != v:
            raise VerifyError("CAL_OFFSET 写 %d 回读 %d" % (v, got))
        return got

    def cal_gain(self):
        return self.rd(REG_OFF["CAL_GAIN"]) & 0xFFFF

    def set_cal_gain(self, v):
        if not 0 <= v <= 0xFFFF:
            raise ValueError("CAL_GAIN 是 16 位无符号 q8.8")
        self.wr_checked(REG_OFF["CAL_GAIN"], v)
        got = self.cal_gain()
        if self.verify and got != v:
            raise VerifyError("CAL_GAIN 写 %d 回读 %d" % (v, got))
        return got

    # ---- 灯环 ----
    @staticmethod
    def grb(r, g, b):
        """把 RGB 三分量装成 reg_bank 的 24 位 GRB（G 在高位，B 在低位）。"""
        for c in (r, g, b):
            if not 0 <= c <= 255:
                raise ValueError("RGB 分量要在 0..255")
        return ((g & 0xFF) << 16) | ((r & 0xFF) << 8) | (b & 0xFF)

    @staticmethod
    def unpack_grb(v):
        return ((v >> 8) & 0xFF, (v >> 16) & 0xFF, v & 0xFF)     # (r, g, b)

    def set_led(self, i, r, g, b):
        if not 0 <= i < LED_NUM:
            raise ValueError("灯序号 0..%d" % (LED_NUM - 1))
        off = LED_BASE + i * LED_STRIDE
        val = self.grb(r, g, b)
        back = self.wr_checked(off, val)
        if self.verify and (back & 0xFF_FFFF) != val:
            raise VerifyError("LED[%d] 写 0x%06X 回读 0x%06X" % (i, val, back & 0xFF_FFFF))
        return val

    def set_all_leds(self, r, g, b):
        for i in range(LED_NUM):
            self.set_led(i, r, g, b)
        self.led_update()

    def led_update(self):
        """LED_UPDATE（0xC0）：写 1 触发刷新；自清，读回恒 0。"""
        self.wr(REG_OFF["LED_UPDATE"], 1)

    # ---- 自检 ----
    def selftest(self, verbose=True):
        """写 -> 回读 闭环自检。返回 True/False。"""
        state = {"ok": True}

        def check(title, cond, detail=""):
            state["ok"] = state["ok"] and bool(cond)
            if verbose:
                print("  [%s] %s%s" % ("PASS" if cond else "FAIL", title,
                                       ("  " + detail) if detail else ""))

        print("== reg_bank 自检 ==")
        # 1) 阈值区：先存原值，逐个写一组有特征的值再比
        saved = self.get_thresholds()
        probe_vals = {"spl_quiet": 451, "spl_warn": 551, "spl_alarm": 651,
                      "conf_th": 61, "conf_hyst": 11, "t_warn": 4, "t_alarm": 16,
                      "t_back_from_warn": 9, "t_back_from_alarm": 21,
                      "th_flat": 401, "th_flux": 151, "th_low": 601,
                      "hyst_spl_warn": 31, "hyst_spl_alarm": 41}
        try:
            for k, v in probe_vals.items():
                self.set_threshold(k, v)
            got = self.get_thresholds()
            bad = [k for k in probe_vals if got.get(k) != probe_vals[k]]
            check("阈值区 %d 个字段写回读" % len(probe_vals), not bad,
                  "" if not bad else "对不上：%s" % bad)
        except (BusError, VerifyError) as e:
            check("阈值区写回读", False, str(e))
        finally:
            for k, v in saved.items():
                try:
                    self.set_threshold(k, v)
                except (BusError, VerifyError):
                    pass

        # 2) 校准寄存器（含负数，验 16 位有符号打包）
        try:
            old_off, old_gain = self.cal_offset(), self.cal_gain()
            r1 = self.set_cal_offset(-35)
            check("CAL_OFFSET 写 -35（-3.5 dB）", r1 == -35, "回读 %d" % r1)
            r2 = self.set_cal_gain(200)
            check("CAL_GAIN 写 200（q8.8）", r2 == 200, "回读 %d" % r2)
            self.set_cal_offset(old_off)
            self.set_cal_gain(old_gain)
        except (BusError, VerifyError) as e:
            check("校准寄存器", False, str(e))

        # 3) LED 区：写一颗，回读，并验 GRB 打包顺序
        try:
            back = self.set_led(0, 0x12, 0x34, 0x56)
            check("LED[0] GRB 打包 (r12 g34 b56) = 0x341256",
                  back == 0x341256, "得到 0x%06X" % back)
            self.set_led(0, 0, 0, 0)
        except (BusError, VerifyError) as e:
            check("LED 区", False, str(e))

        # 4) 统计暂停位是电平量，回读应当保持
        try:
            on = self.set_pause(True)
            off = self.set_pause(False)
            check("CTRL 统计暂停位可置可清", on is True and off is False)
        except (BusError, VerifyError) as e:
            check("CTRL 统计暂停位", False, str(e))

        # 5) 只读遥测：读得到 + 四麦/DOA 字段齐（0xC4–0xE4、0xE8–0xF8）就算通路通
        try:
            snap = self.snapshot()
            mic_ok = len(snap.get("mic_rms") or []) == 4 and len(snap.get("mic_peak") or []) == 4
            doa_ok = ("doa_angle_deg" in snap) and ("doa_solve_cnt" in snap)
            check("遥测区可读（含四麦 0xC4–0xE4 与 DOA 0xE8–0xF8）", mic_ok and doa_ok,
                  "SPL=%.1f dB(A) 状态=%s 方位=%d 度 解算 %d 次"
                  % (snap["spl_now_db"], snap["state_name"], snap["doa_angle_deg"],
                     snap["doa_solve_cnt"]))
        except BusError as e:
            check("遥测区", False, str(e))

        # 6) 事件 FIFO：空读应当拿到 0（标记不是 0xA5），不该是总线错误
        try:
            ev = self.next_event()
            check("EVENT_FIFO 空读处理", True, "空" if ev is None else ev["name"])
        except BusError as e:
            check("EVENT_FIFO", False, str(e))

        print("== 结论：%s ==" % ("全部通过" if state["ok"] else "有失败项"))
        return state["ok"]

    def check_factory_defaults(self, verbose=True):
        """回读 PL 的阈值/校准默认值，与 §5.3 出厂值比对。"""
        if verbose:
            print("== 出厂值比对（§5.3 thresholds.json）==")
        th = self.get_thresholds()
        bad = []
        for k, want in FACTORY_TH.items():
            got = th.get(k)
            if got != want:
                bad.append((k, want, got))
            if verbose:
                print("  %-20s 期望 %5d  实测 %5s  %s"
                      % (k, want, got, "OK" if got == want else "差异"))
        for name, want, got in (("CAL_OFFSET", FACTORY_CAL_OFFSET, self.cal_offset()),
                                ("CAL_GAIN", FACTORY_CAL_GAIN, self.cal_gain())):
            if got != want:
                bad.append((name, want, got))
            if verbose:
                print("  %-20s 期望 %5d  实测 %5d  %s"
                      % (name, want, got, "OK" if got == want else "差异"))
        if bad and verbose:
            names = [b[0] for b in bad]
            print("  差异项：%s" % ", ".join(names))
            print("  提示：阈值区名字与语义的对照见文件头「阈值区」那段注释。")
        return not bad

# ======================================================================
# 圆屏帧存（§4.10，经 AXI BRAM Controller 写）
#   PIX_BITS = 16（RGB565），每 32 位字装 2 个像素，字内小端：
#     字节 0/1 = 第 0 个像素的低/高字节，字节 2/3 = 第 1 个像素。
#   所以 PS 只要按"像素顺序、每像素两字节、低字节在前"排好，直接贴过去即可。
# ======================================================================
class Framebuf:
    W = 240
    H = 240
    PIXELS = W * H
    BYTES = PIXELS * 2

    def __init__(self, backend, base_off=FB_OFF, verify=True):
        self.b = backend
        self.base = base_off
        self.verify = verify

    @staticmethod
    def rgb565(r, g, b):
        return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)

    def word_addr(self, x, y):
        """像素坐标 -> (帧存字地址, 字内字节偏移)，给局部刷新/调试用。"""
        if not (0 <= x < self.W and 0 <= y < self.H):
            raise ValueError("像素坐标越界 (%d,%d)" % (x, y))
        pix = y * self.W + x
        return pix // 2, (pix % 2) * 2

    def poke(self, x, y, px565):
        """只改一个像素（读-改-写），用来画光标或局部刷新。"""
        wa, bo = self.word_addr(x, y)
        off = self.base + wa * 4
        old = self.b.read32(off)
        new = (old & ~(0xFFFF << (bo * 8))) | ((px565 & 0xFFFF) << (bo * 8))
        self.b.write32(off, new)
        return new

    def _put(self, buf, x, y, px):
        o = (y * self.W + x) * 2
        buf[o] = px & 0xFF
        buf[o + 1] = (px >> 8) & 0xFF

    def _draw_testpattern(self, buf):
        """标准测试卡：外框 + 对角线 + 顶部 8 段彩条 + 左侧灰阶 + 中心十字。"""
        W, H = self.W, self.H
        white = self.rgb565(255, 255, 255)
        for x in range(W):
            self._put(buf, x, 0, white)
            self._put(buf, x, H - 1, white)
        for y in range(H):
            self._put(buf, 0, y, white)
            self._put(buf, W - 1, y, white)
            self._put(buf, y, y, white)
            self._put(buf, W - 1 - y, y, white)
        bars = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
                (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
        bw = W // 8
        for i, (r, g, b) in enumerate(bars):
            px = self.rgb565(r, g, b)
            for y in range(10, 40):
                for x in range(i * bw, min(W, (i + 1) * bw)):
                    self._put(buf, x, y, px)
        for y in range(50, 210):
            px = self.rgb565(y, y, y)
            for x in range(10, 40):
                self._put(buf, x, y, px)
        cx, cy = W // 2, H // 2
        for d in range(-20, 21):
            self._put(buf, cx + d, cy, white)
            self._put(buf, cx, cy + d, white)

    def build_frame(self, pattern="solid", color=(0, 0, 0)):
        """在内存里拼一整帧 RGB565 字节流（2 字节/像素，低字节在前）。"""
        buf = bytearray(self.BYTES)
        if pattern == "solid":
            buf[:] = struct.pack("<H", self.rgb565(*color)) * self.PIXELS
        elif pattern == "testpattern":
            self._draw_testpattern(buf)
        else:
            raise ValueError("未知图案：%s" % pattern)
        return bytes(buf)

    def write_frame(self, data, offset=0):
        if len(data) % 4:
            raise ValueError("整帧字节数必须是 4 的倍数（否则末字写不全）")
        if offset % 4:
            raise ValueError("偏移必须 4 字节对齐（AXI BRAM 按字写）")
        if offset + len(data) > FB_BYTES:
            raise ValueError("超出帧存范围：%d + %d > %d" % (offset, len(data), FB_BYTES))
        self.b.write(self.base + offset, data)

    def read_frame(self, offset=0, n=FB_BYTES):
        return self.b.read(self.base + offset, n)

    def fill(self, color=(0, 0, 0)):
        self.write_frame(self.build_frame("solid", color))

    def testpattern(self):
        self.write_frame(self.build_frame("testpattern"))

    def check_roundtrip(self, verbose=True):
        """回读帧存，验证 PS -> AXI BRAM Controller -> lcd_framebuf 写通路。

        只和刚写进去的字节比，不看屏上画得像不像 —— 这样"通路问题"和
        "屏/初始化问题"能分开定位。
        """
        pat = self.build_frame("testpattern")
        self.write_frame(pat)
        back = self.read_frame()
        bad = 0
        first = None
        for i in range(0, self.BYTES, 4):
            if back[i:i + 4] != pat[i:i + 4]:
                bad += 1
                if first is None:
                    first = i
        ok = bad == 0
        if verbose:
            print("== 帧存写回读比对 ==")
            print("  写入 %d B，逐字比对 %d 个字，不一致 %d 个"
                  % (self.BYTES, self.BYTES // 4, bad))
            if not ok:
                print("  首个不一致字节偏移 0x%X" % first)
            print("  [%s] AXI BRAM Controller -> lcd_framebuf 写通路"
                  % ("PASS" if ok else "FAIL"))
        return ok

    def probe_window(self):
        """在子进程里探帧存窗口的头/尾字能不能读（诊断地址窗口用）。"""
        return [(off, self.b.probe(off)) for off in
                (self.base, self.base + self.BYTES - 4, self.base + FB_WINDOW - 4)]

# ======================================================================
# 打印
# ======================================================================
def print_snapshot(snap):
    print("  状态        %-6s（已持续 %d s）" % (snap["state_name"], snap["state_dwell_sec"]))
    print("  当前 SPL    %.1f dB(A)   人声置信度 %d"
          % (snap["spl_now_db"], snap["conf_now"]))
    print("  10s 均值/峰值 %.1f / %.1f dB(A)   60s 均值 %.1f dB(A)"
          % (snap["spl_avg_10s_db10"] / 10.0, snap["spl_peak_10s_db10"] / 10.0,
             snap["spl_avg_60s_db10"] / 10.0))
    print("  人声占比    %d%%   安静指数 %d"
          % (snap["speech_ratio"], snap["quiet_index"]))
    print("  干扰事件    %d 次   有人时长 %d s" % (snap["disturb_cnt"], snap["occupy_sec"]))
    print("  忙标志      FFT=%d 刷屏=%d 灯环=%d   FIFO 溢出=%d 丢弃=%d"
          % (snap["fft_busy"], snap["lcd_busy"], snap["led_busy"],
             snap["ev_overflow"], snap["ev_drop_cnt"]))
    # 四麦与 DOA 遥测（0xC4–0xE4、0xE8–0xF8）：有就打印，数据源没给就跳过
    mic = snap.get("mic_rms")
    if mic:
        pk = snap.get("mic_peak") or [0, 0, 0, 0]
        dom = snap.get("mic_dom_idx", 0)
        print("  四路麦能量  " + "  ".join("麦%d %5d(%.3f)%s"
              % (i + 1, mic[i], mic[i] / 256.0, "<-最强" if i == dom else "")
              for i in range(4)))
        print("  四路麦峰值  " + "  ".join("麦%d %5d" % (i + 1, pk[i]) for i in range(4))
              + "   优势标志 %d" % (1 if snap.get("mic_dom_flag") else 0))
    if "doa_angle_deg" in snap:
        print("  声源方位    %3d 度   有效 %d  新鲜 %d   X/Y 时延 %+.3f / %+.3f 采样点"
              % (snap["doa_angle_deg"], 1 if snap.get("doa_ok") else 0,
                 1 if snap.get("doa_fresh") else 0,
                 snap.get("doa_delay_x") or 0.0, snap.get("doa_delay_y") or 0.0))
        print("  互相关峰    X=%5d  Y=%5d   本次读到解算 %d 次"
              % (snap.get("doa_peak_x", 0), snap.get("doa_peak_y", 0),
                 snap.get("doa_solve_cnt", 0)))


def open_backend(args):
    if args.mock:
        return MockBackend(REG_BASE, WINDOW_SIZE)
    return MmapBackend(args.base, WINDOW_SIZE)


def parse_kv(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit("--set 要写成 名字=值，例如 --set spl_warn=580")
        k, v = it.split("=", 1)
        out[k.strip()] = int(v.strip(), 0)
    return out


def hex_rgb(s):
    h = s.lstrip("#")
    if len(h) != 6:
        raise SystemExit("颜色要写成 RRGGBB，例如 00FF00")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ======================================================================
# 命令行
# ======================================================================
def build_parser():
    ap = argparse.ArgumentParser(
        description="静域 QuietZone PS 侧寄存器访问层（§4.11 / §4.10）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="可用阈值名：\n  " + ", ".join(TH_NAMES))
    ap.add_argument("--base", type=lambda s: int(s, 0), default=REG_BASE,
                    help="寄存器窗口物理基址，默认 0x40000000")
    ap.add_argument("--mock", action="store_true",
                    help="用内置模型代替 /dev/mem（PC 上验代码用）")
    ap.add_argument("--dump", action="store_true", help="打印一次遥测 + 阈值 + 事件")
    ap.add_argument("--watch", type=int, metavar="N", help="每秒打印一次遥测，共 N 次")
    ap.add_argument("--selftest", action="store_true", help="写->回读闭环自检")
    ap.add_argument("--check-defaults", action="store_true",
                    help="把回读到的阈值/校准与 §5.3 出厂值比对")
    ap.add_argument("--soft-reset", action="store_true", help="CTRL 软复位（PL 声学链）")
    ap.add_argument("--clear-stats", action="store_true", help="CTRL 统计清零")
    ap.add_argument("--pause", choices=["on", "off"], help="统计暂停位")
    ap.add_argument("--set", action="append", metavar="名称=值", help="写阈值，可重复")
    ap.add_argument("--cal-offset", type=int, help="SPL 校准偏移（dB x10，可负）")
    ap.add_argument("--cal-gain", type=int, help="SPL 校准增益（q8.8，256 = x1.0）")
    ap.add_argument("--led", metavar="RRGGBB", help="16 颗灯写成同一颜色并刷新")
    ap.add_argument("--fb-fill", metavar="RRGGBB", help="整帧填一个颜色")
    ap.add_argument("--fb-pattern", action="store_true", help="往帧存画标准测试卡")
    ap.add_argument("--fb-check", action="store_true", help="帧存写回读比对")
    ap.add_argument("--probe", action="store_true", help="探测关键地址有没有被应答")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not any([args.dump, args.watch, args.selftest, args.check_defaults,
                args.soft_reset, args.clear_stats, args.pause, args.set,
                args.cal_offset is not None, args.cal_gain is not None,
                args.led, args.fb_fill, args.fb_pattern, args.fb_check, args.probe]):
        build_parser().print_help()
        return 0

    be = open_backend(args)
    print("后端：%s" % be.name())
    reg = RegBank(be)
    fb = Framebuf(be)
    ok = True

    try:
        if args.probe:
            print("== 地址探测（每个地址在子进程里读，SIGBUS 打不到本进程）==")
            for nm in ("CTRL", "STATUS", "SPL_NOW", "EV_DROP_CNT",
                       "LED_COLOR_0", "LED_UPDATE"):
                off = REG_OFF[nm]
                print("  REG  %-14s +0x%03X  %s"
                      % (nm, off, "应答" if be.probe(off) else "无应答/总线错误"))
            for off, r in fb.probe_window():
                print("  FB   +0x%05X           %s"
                      % (off, "应答" if r else "无应答/总线错误"))

        if args.soft_reset:
            reg.soft_reset()
            print("已发软复位脉冲（只复位 PL 声学链，寄存器内容保持不变）")

        if args.clear_stats:
            reg.stat_clear()
            print("已发统计清零脉冲（含 EV_DROP_CNT 与 FIFO 溢出标志）")

        if args.pause:
            print("统计暂停 = %s" % reg.set_pause(args.pause == "on"))

        if args.set:
            for k, v in parse_kv(args.set).items():
                got = reg.set_threshold(k, v)
                print("阈值 %-20s <- %6d  回读 %6d  %s"
                      % (k, v, got, "OK" if got == v else "不一致"))
                ok = ok and got == v

        if args.cal_offset is not None:
            print("CAL_OFFSET <- %d  回读 %d"
                  % (args.cal_offset, reg.set_cal_offset(args.cal_offset)))
        if args.cal_gain is not None:
            print("CAL_GAIN   <- %d  回读 %d"
                  % (args.cal_gain, reg.set_cal_gain(args.cal_gain)))

        if args.led:
            r, g, b = hex_rgb(args.led)
            reg.set_all_leds(r, g, b)
            print("灯环 <- RGB(%d,%d,%d)，已触发刷新" % (r, g, b))

        if args.fb_fill:
            r, g, b = hex_rgb(args.fb_fill)
            fb.fill((r, g, b))
            print("帧存 <- 整帧 RGB(%d,%d,%d)" % (r, g, b))

        if args.fb_pattern:
            fb.testpattern()
            print("帧存 <- 标准测试卡（%d B）" % fb.BYTES)

        if args.fb_check:
            ok = fb.check_roundtrip() and ok

        if args.dump:
            print("== 遥测 ==")
            print_snapshot(reg.snapshot())
            print("== 阈值 ==")
            th = reg.get_thresholds()
            for k in TH_NAMES:
                print("  %-20s %6d" % (k, th[k]))
            print("  %-20s %6d  (q8.8 = %.3f)"
                  % ("cal_gain", reg.cal_gain(), reg.cal_gain() / 256.0))
            print("  %-20s %6d" % ("cal_offset", reg.cal_offset()))
            evs = reg.drain_events()
            if evs:
                print("== 事件（已出队）==")
                for e in evs:
                    print("  码 %d %-18s 之后状态 %s"
                          % (e["code"], e["name"], e["level_after_name"]))

        if args.check_defaults:
            ok = reg.check_factory_defaults() and ok

        if args.selftest:
            ok = reg.selftest() and ok

        if args.watch:
            print("== 每秒一次，共 %d 次（Ctrl-C 退出）==" % args.watch)
            for _ in range(args.watch):
                print(time.strftime("  %H:%M:%S"))
                print_snapshot(reg.snapshot())
                for e in reg.drain_events():
                    print("    事件 %-18s 之后状态 %s"
                          % (e["name"], e["level_after_name"]))
                time.sleep(1)
    except BusError as e:
        print("总线错误：%s" % e)
        return 3
    except VerifyError as e:
        print("回读校验失败：%s" % e)
        return 4
    except KeyboardInterrupt:
        print("")
    finally:
        be.close()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
