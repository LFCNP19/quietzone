#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静域 QuietZone · 统计网页服务（PS 侧）

用途：把 §4.11 寄存器堆里的统计量变成一张管理视角的网页。
      页面上每一个数字都来自寄存器，字段名与 `hw_ifc.REG_OFF` 一一对应，
      不做二次估算；只有"展示层阈值"（见 DISPLAY_TH）是本文件自己定义的，
      在页面上明确标注，不与 PL 的 TH_* 混为一谈。

三种数据源：
  --source serial  sound_loc 真实板卡：CP210x UART 的 QZ CSV 遥测（默认）
  --source board   历史 QuietZone PL 寄存器：/dev/mem + mmap（兼容保留）
  --source demo    无板卡时的脚本化演示

用法：
  python stats_server.py --source serial --serial-port COM5
  python stats_server.py --source serial                # 自动找 CP210x
  python stats_server.py --source demo --port 8088
"""

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import random
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import hw_ifc  # noqa: E402  同目录的寄存器访问层（031 轮）

PROJECT_ROOT = os.path.dirname(HERE)
WEB_DIR = os.path.join(HERE, "web")

# ----------------------------------------------------------------------
# 展示层阈值：这些不是 PL 里的 TH_*，是本文件为了让"管理人员看到需要关注的事"
# 而定义的判读口径。页面上会标出来源，改这里不需要动 RTL。
# ----------------------------------------------------------------------
DISPLAY_TH = {
    "speech_ratio_high": 70,      # 10 s 内判为人声的帧占比 >= 70% -> 持续交谈
    "speech_ratio_mid": 40,       # 中途交代用
    "conf_active": 60,            # 与 PL 的 conf_th 出厂值一致（用于说明当前帧是否"人声"）
    "disturb_burst": 3,           # 近 10 分钟内新增干扰事件 >= 3 次 -> 频发
    "occupy_long_sec": 1800,      # 连续占座 >= 30 分钟仍出声 -> 提示
    "mic_dead_db10": 300,         # SPL 连续 <= 30.0 dB(A) -> 疑似麦克风没接
    "mic_dead_samples": 20,       # 连续样本数（0.5 s 一次 -> 10 s）
    "alarm_dwell_warn_sec": 120,  # ALARM 停留 >= 120 s（§4.7 的续报阈值）-> 提醒
    "dwell_stale_sec": 30,       # STATE_DWELL 30 s 不变化 -> PL 侧统计已停摆
    # ---- 四麦遥测区（0xC4–0xE4，040 轮）----
    "mic_imb_ratio": 4.0,        # 四路窗能量最大/最小 >= 4 倍（约 12 dB）-> 某一路明显异常
    "mic_imb_samples": 20,       # 连续样本数（0.5 s 一次 -> 10 s）
    "mic_quiet_raw": 16,         # 四路能量都 <= 16（Q8.8 的 0.0625）-> 四路都没信号
    # ---- DOA 遥测区（0xE8–0xF8，048 轮，050 轮接入真实信号）----
    "doa_stale_sec": 30,         # DOA_CNT 30 s 不增长 -> 方位解算停摆
    "doa_bias_deg": 120,         # |方位角| 持续 >= 120 度 -> 声源长期在同一侧
    "doa_bias_sec": 60,          # 上述条件持续秒数
}

# 事件码 -> 人话（与 hw_ifc.EV_NAMES 同源，这里补中文）
EV_TEXT = {
    "occupy_on": ("有人落座", "info"),
    "occupy_off": ("人员离开", "info"),
    "to_warn": ("进入 WARN（偏吵）", "warning"),
    "to_alarm_burst": ("突发吵闹 -> ALARM", "critical"),
    "to_alarm_speech_15s": ("持续交谈 15 s -> ALARM", "critical"),
    "back_quiet": ("回落至安静", "ok"),
    "back_warn": ("由吵闹降为偏吵", "warning"),
    "alarm_ongoing": ("吵闹仍在持续（续报）", "critical"),
    "uart_to_idle": ("进入待机状态", "info"),
    "uart_to_quiet": ("回到安静区", "ok"),
    "uart_to_warn": ("进入注意区", "warning"),
    "uart_to_alarm": ("进入干扰区", "critical"),
    "none": ("空事件", "info"),
}

STATE_COLOR = {"IDLE": "idle", "QUIET": "quiet", "WARN": "warn", "ALARM": "alarm"}


def iso(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts else time.time()))


def aggregate_calendar(date_text, records, bin_minutes=15):
    """把某一天的真实采样聚合为分时噪音数据。

    records 同时接受实时内存记录和 qz-YYYY-MM-DD.jsonl 中持久化的记录。
    空时间档保留为 null，前端因此能明确区分「没有数据」与「测得 0 dB」。
    """
    if bin_minutes <= 0 or 1440 % bin_minutes:
        raise ValueError("bin_minutes 必须能整除 1440")
    bin_count = 1440 // bin_minutes
    buckets = [{"n": 0, "sum": 0.0, "max": None, "warn": 0, "alarm": 0}
               for _ in range(bin_count)]
    total_n = 0
    total_sum = 0.0
    total_max = None
    latest_iso = None

    for rec in records:
        rec_iso = str(rec.get("iso", ""))
        if rec_iso[:10] != date_text:
            continue
        try:
            minute_of_day = int(rec_iso[11:13]) * 60 + int(rec_iso[14:16])
            spl = float(rec.get("spl_now_db"))
        except (TypeError, ValueError):
            continue
        bucket = buckets[min(bin_count - 1, minute_of_day // bin_minutes)]
        state = rec.get("state_name", "")
        bucket["n"] += 1
        bucket["sum"] += spl
        bucket["max"] = spl if bucket["max"] is None else max(bucket["max"], spl)
        bucket["warn"] += int(state in ("WARN", "ALARM"))
        bucket["alarm"] += int(state == "ALARM")
        total_n += 1
        total_sum += spl
        total_max = spl if total_max is None else max(total_max, spl)
        latest_iso = rec_iso if latest_iso is None else max(latest_iso, rec_iso)

    bins = []
    equivalent_attention_minutes = 0.0
    for index, bucket in enumerate(buckets):
        start_min = index * bin_minutes
        end_min = start_min + bin_minutes
        start_text = "%02d:%02d" % (start_min // 60, start_min % 60)
        end_text = "%02d:%02d" % ((end_min // 60) % 24, end_min % 60)
        item = {
            "time": start_text,
            "range": "%s–%s" % (start_text, end_text),
            "samples": bucket["n"],
            "spl_avg": None,
            "spl_peak": None,
            "warn_pct": None,
            "alarm_pct": None,
        }
        if bucket["n"]:
            attention_ratio = (bucket["warn"] / float(bucket["n"]))
            item.update({
                "spl_avg": round(bucket["sum"] / bucket["n"], 1),
                "spl_peak": round(bucket["max"], 1),
                "warn_pct": round(attention_ratio * 100.0, 1),
                "alarm_pct": round(bucket["alarm"] * 100.0 / bucket["n"], 1),
            })
            equivalent_attention_minutes += attention_ratio * bin_minutes
        bins.append(item)

    return {
        "date": date_text,
        "timezone": "Asia/Shanghai",
        "bin_minutes": bin_minutes,
        "summary": {
            "samples": total_n,
            "avg_spl": round(total_sum / total_n, 1) if total_n else None,
            "peak_spl": round(total_max, 1) if total_max is not None else None,
            "attention_minutes": round(equivalent_attention_minutes, 1),
            "active_bins": sum(1 for item in bins if item["samples"]),
            "latest_iso": latest_iso,
        },
        "bins": bins,
    }


# 四麦阵几何（与 src/ps/main.c 一致）：对角线时延的物理标称值。
MIC_D_M = 0.05                         # 正方形阵边长（米）
C_SOUND_MPS = 343.0                    # 声速（米/秒）
FS_HZ = 48828.125                      # 帧率 = 50 MHz / 1024
TAU_DIAG = MIC_D_M * math.sqrt(2.0) / C_SOUND_MPS * FS_HZ   # ≈ 10.07 采样点
DOA_WINDOW_S = 30.0                    # 多帧复核窗口（秒）：每次短促声只出 1~2 帧，需跨几次攒够
DOA_AGREE_DEG = 25.0                   # 认定为同一方位的离散度上限


def aggregate_doa(events, sector_degrees=10):
    """把可信定位事件聚合为环形的角度频次。角度约定 0°=东，逆时针为正。"""
    sector_degrees = int(sector_degrees)
    if sector_degrees <= 0 or 360 % sector_degrees:
        raise ValueError("sector_degrees 必须能整除 360")
    count_sectors = 360 // sector_degrees
    counts = [0] * count_sectors
    accepted = 0

    for event in events:
        if "trust" in event and not event.get("trust"):
            continue
        try:
            angle = float(event.get("angle_deg"))
        except (TypeError, ValueError):
            continue
        angle = ((angle + 180.0) % 360.0) - 180.0
        index = int((angle + 180.0) // sector_degrees) % count_sectors
        counts[index] += 1
        accepted += 1

    peak_index = max(range(count_sectors), key=lambda index: counts[index]) if accepted else None
    peak_count = counts[peak_index] if peak_index is not None else 0
    sectors = []
    for index, count in enumerate(counts):
        start = -180.0 + index * sector_degrees
        end = start + sector_degrees
        center = start + sector_degrees / 2.0
        if center >= 180.0:
            center -= 360.0
        sectors.append({
            "start": start,
            "end": end,
            "center": center,
            "count": count,
            "pct": round(count * 100.0 / accepted, 1) if accepted else 0.0,
        })

    peak_center = sectors[peak_index]["center"] if peak_index is not None else None
    return {
        "sector_degrees": sector_degrees,
        "samples": accepted,
        "peak_sector": peak_index,
        "peak_center": peak_center,
        "peak_pct": round(peak_count * 100.0 / accepted, 1) if accepted else None,
        "sectors": sectors,
    }


# ======================================================================
# 数据源 1：真实板卡
# ======================================================================
def parse_qz_line(line):
    """解析固件输出的机器可读行：QZ,key=value,...。

    这一步只做协议转换，不参与定位或响度计算。无法识别的行返回 None，
    使启动横幅、人类可读的 AZ 行和调试信息可以安全地共用同一 UART。
    """
    text = line.decode("ascii", "ignore") if isinstance(line, bytes) else str(line)
    text = text.strip()
    if not text.startswith("QZ,"):
        return None
    out = {}
    for item in text.split(",")[1:]:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        try:
            out[key] = int(value.strip(), 10)
        except ValueError:
            continue
    required = ("kind", "seq", "az10", "spl10", "state", "trust")
    if any(k not in out for k in required):
        return None
    if out["state"] not in (0, 1, 2, 3):
        return None
    return out


def find_serial_port():
    """优先自动找 CP210x（AX7020 板载 USB-UART）。"""
    if serial is None:
        raise RuntimeError("缺少 pyserial；请运行 python -m pip install pyserial")
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        raise RuntimeError("没有发现串口设备")
    for port in ports:
        hay = ("%s %s %s" % (port.device, port.description, port.hwid)).upper()
        if "CP210" in hay or "10C4" in hay or "SILICON LABS" in hay:
            return port.device
    return ports[0].device


def _norm_mic(raw):
    """把快照里的四麦字段归一成页面用的形状；数据源没给这个字段时返回 None。

    口径与 hw_ifc.RegBank.snapshot() 一一对应：
      mic_rms[i]   = MIC_RMS{1..4}（0xC8–0xD4，Q8.8 窗能量）
      mic_peak[i]  = MIC_PK{1..4}（0xD8–0xE4，窗内峰值）
      mic_dom_idx  = MIC_DOM（0xC4）的 bit1:0，mic_dom_flag = bit2
    """
    db10 = raw.get("mic_rms_db10")
    if db10:
        try:
            db10 = [int(v) for v in db10]
        except (TypeError, ValueError):
            return None
        mx_db = max(db10)
        amps = [10.0 ** (v / 200.0) for v in db10]
        mx_amp = max(amps) or 1.0
        return {
            "rms_raw": [max(0, int(round(a * 256.0))) for a in amps],
            "rms_db10": db10,
            "rms_pct": [round(a * 100.0 / mx_amp, 1) for a in amps],
            "peak": [int(v) for v in (raw.get("mic_peak") or [0, 0, 0, 0])],
            "dom": int(raw.get("mic_dom_idx", db10.index(mx_db))) & 0x3,
            "dom_flag": (sorted(db10)[-1] - sorted(db10)[-2]) >= 60,
            "imb_ratio": round(10.0 ** ((mx_db - min(db10)) / 200.0), 2),
            "imb_db": round((mx_db - min(db10)) / 10.0, 1),
            "unit": "dBFS",
        }

    rms = raw.get("mic_rms")
    if not rms:
        return None
    try:
        rms = [int(v) & 0xFFFF for v in rms]
        peak = [int(v) & 0xFFFF for v in (raw.get("mic_peak") or [0, 0, 0, 0])]
    except (TypeError, ValueError):
        return None
    mx = max(rms) or 1
    mn = min(rms)
    return {
        "rms_raw": rms,
        "rms_db10": None,
        "rms_pct": [round(v * 100.0 / mx, 1) for v in rms],
        "peak": peak,
        "dom": int(raw.get("mic_dom_idx", 0)) & 0x3,
        "dom_flag": bool(raw.get("mic_dom_flag", False)),
        "imb_ratio": round(mx / float(max(1, mn)), 2),
        "imb_db": None,
        "unit": "Q8.8",
    }


# 固件 trust 字段的打包位定义（见 main.c telemetry_line）：
#   bit0      本帧自检通过
#   [7:4]     有效麦克风掩码，bit i = CHi 有信号
#   [15:8]    最小二乘归一化残差 x100（0..255）
#   [23:16]   实测孔径 A/TAU_SIDE x100（255 饱和）
def doa_trust_bits(word):
    w = int(word or 0) & 0xFFFFFFFF
    return {
        "ok": bool(w & 1),
        "mask": (w >> 4) & 0xF,
        "resid_pct": (w >> 8) & 0xFF,
        "ap_pct": (w >> 16) & 0xFF,
    }


# 固件 doa_solve() 的配对优先级，用来把 t02/t13 还原成"哪两路之间"
DOA_PAIR_ORDER = ((0, 2), (1, 3), (0, 1), (1, 2), (0, 3), (2, 3))


def doa_pairs_for_mask(mask):
    if mask & 0xF == 0xF:
        return [(0, 2), (1, 3)]
    return [p for p in DOA_PAIR_ORDER if (mask >> p[0]) & 1 and (mask >> p[1]) & 1]


def _norm_doa(raw):
    """把快照里的 DOA 字段归一成页面用的形状；数据源没给这个字段时返回 None。

    口径与 hw_ifc.RegBank.snapshot() 一一对应：
      doa_angle_deg = DOA_ANGLE（0xE8）的 bit8:0（整数度 0–359）
      doa_ok / doa_fresh = bit9 / bit10（bit10 读后清零）
      doa_delay_x/y = 0xEC / 0xF0（Q8.8 采样点，有符号）
      doa_solve_cnt = DOA_CNT（0xF8，读后清零的解算次数）
    """
    if "doa_angle_deg" not in raw:
        return None
    signed = bool(raw.get("doa_signed", False))
    angle = float(raw.get("doa_angle_deg", 0))
    if signed:
        angle = ((angle + 180.0) % 360.0) - 180.0
    else:
        angle = int(angle) & 0x1FF
    return {
        "angle": angle,
        "signed": signed,
        "has_value": bool(raw.get("doa_has_value", False)),
        "ok": bool(raw.get("doa_ok", False)),
        "fresh": bool(raw.get("doa_fresh", False)),
        "age_sec": raw.get("doa_age_sec"),
        "seq": int(raw.get("doa_seq", 0)),
        "delay_x": raw.get("doa_delay_x"),
        "delay_y": raw.get("doa_delay_y"),
        "peak_x": int(raw.get("doa_peak_x", 0)),
        "peak_y": int(raw.get("doa_peak_y", 0)),
        "solve_cnt": int(raw.get("doa_solve_cnt", 0)),
        "t02": raw.get("doa_t02"),
        "t13": raw.get("doa_t13"),
        "state": raw.get("doa_state"),
        "conf": raw.get("doa_conf"),
        "frames": raw.get("doa_frames"),
        "spread_deg": raw.get("doa_spread_deg"),
        "amp": raw.get("doa_amp"),
        "cluster_angle": raw.get("doa_cluster_angle_deg"),
        "host_total": raw.get("doa_host_total"),
        "thr_dbfs10": raw.get("gate_thr_dbfs10"),
        "floor_dbfs10": raw.get("gate_floor_dbfs10"),
        "mask": raw.get("doa_mic_mask"),
        "resid_pct": raw.get("doa_resid_pct"),
        "ap_ratio": raw.get("doa_ap_ratio"),
        "pair_0": raw.get("doa_pair_0"),
        "pair_1": raw.get("doa_pair_1"),
    }


class BoardSource:
    mode = "board"
    label = "板卡实时"

    def __init__(self, base=hw_ifc.REG_BASE):
        self.backend = hw_ifc.MmapBackend(phys_base=base)
        self.regs = hw_ifc.RegBank(self.backend, verify=False)
        self.addr = "0x%08X" % base
        self.note = "/dev/mem + mmap，寄存器窗口 64 KB，帧存窗口 0x%08X" % hw_ifc.FB_BASE

    def thresholds(self):
        return self.regs.get_thresholds()

    def read(self):
        """返回 (snapshot, events)；events 是本次读走的事件列表（读即出队）。"""
        snap = self.regs.snapshot()
        evs = []
        for e in self.regs.drain_events(limit=16):
            e = dict(e)
            e["ts"] = time.time()
            evs.append(e)
        return snap, evs

    def is_paused(self):
        return self.regs.pause()

    def control(self, action):
        if action == "pause":
            self.regs.set_pause(True)
        elif action == "resume":
            self.regs.set_pause(False)
        elif action == "clear":
            self.regs.stat_clear()
        elif action == "soft_reset":
            self.regs.soft_reset()
        else:
            return False, "未知操作 %s" % action
        return True, "已下发 %s" % action

    def close(self):
        self.backend.close()


# ======================================================================
# 数据源 2：sound_loc 串口接口（真实测量）
# ======================================================================
class SerialSource:
    mode = "serial"
    label = "四麦定位板 · UART"

    def __init__(self, port=None, baud=115200):
        if serial is None:
            raise RuntimeError("缺少 pyserial；请运行 python -m pip install pyserial")
        self.port = port or find_serial_port()
        self.baud = int(baud)
        self.addr = "%s @ %d" % (self.port, self.baud)
        if str(self.port).startswith("socket://"):
            self.label = "四麦定位板 · JTAG 内存遥测"
            self.note = "经 USB SLAVE/JTAG 读取固件遥测区；方位为 -180°…+180°，0°=东、90°=北"
        else:
            self.note = "固件 QZ CSV 遥测；方位为 -180°…+180°，0°=东、90°=北"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._error = None
        self._records = deque(maxlen=3600)       # 约 75 s @ 48.8 Hz
        self._doa_records = deque(maxlen=7200)   # 每次可信/待判方位帧，供现场标定复盘
        self._last_doa_key = None
        self._last_doa = None                    # 最近一次定位帧；静默心跳不得把它清成 0°
        self._last_doa_rx = 0.0
        self._last = None
        self._last_rx = 0.0
        self._last_seq = None
        self._last_state = None
        self._state_since = time.time()
        self._occupy_s = 0.0
        self._disturb_cnt = 0
        self._solve_pending = 0
        self._solve_total = 0
        self._events = deque(maxlen=100)
        self._parsed = 0
        self._ignored = 0
        self._rx = serial.serial_for_url(self.port, self.baud, timeout=0.25)
        self._rx.reset_input_buffer()
        self._thread = threading.Thread(target=self._reader, name="qz-uart", daemon=True)
        self._thread.start()

    def _reader(self):
        while not self._stop.is_set():
            try:
                line = self._rx.readline()
                if not line:
                    continue
                rec = parse_qz_line(line)
                if rec is None:
                    with self._lock:
                        self._ignored += 1
                    continue
                with self._lock:
                    self._accept(rec)
            except Exception as exc:
                with self._lock:
                    self._error = "%s: %s" % (type(exc).__name__, exc)
                time.sleep(0.2)

    def _accept(self, rec):
        now = time.time()
        state = int(rec["state"])
        prev_state = self._last_state
        prev_ts = self._last_rx
        if prev_state is not None and prev_ts > 0:
            dt = max(0.0, min(1.0, now - prev_ts))
            if prev_state >= 2:
                self._occupy_s += dt
        if prev_state is not None and prev_state < 2 <= state:
            self._disturb_cnt += 1
        if prev_state != state:
            self._state_since = now
            names = ("uart_to_idle", "uart_to_quiet", "uart_to_warn", "uart_to_alarm")
            names_cn = ("IDLE", "QUIET", "WARN", "ALARM")
            self._events.append({
                "ts": now, "code": 0x51, "name": names[state],
                "level_after": state, "level_after_name": names_cn[state],
            })
        self._last_state = state
        self._last = dict(rec)
        self._last_rx = now
        if int(rec["kind"]) == 1:
            key = (int(rec.get("seq", 0)), int(rec.get("az10", 0)),
                   int(rec.get("trust", 0)), int(rec.get("t02_100", 0)),
                   int(rec.get("t13_100", 0)))
            if key != self._last_doa_key:
                self._last_doa_key = key
                self._last_doa = dict(rec)
                self._last_doa_rx = now
                _bits = doa_trust_bits(rec.get("trust", 0))
                self._doa_records.append({
                    "ts": round(now, 3), "iso": iso(now),
                    "seq": int(rec.get("seq", 0)),
                    "angle_deg": int(rec.get("az10", 0)) / 10.0,
                    "trust": int(_bits["ok"]),
                    "mask": _bits["mask"],
                    "resid": _bits["resid_pct"],
                    "ap": _bits["ap_pct"],
                    "t02": int(rec.get("t02_100", 0)) / 100.0,
                    "t13": int(rec.get("t13_100", 0)) / 100.0,
                    "spl_db10": int(rec.get("spl10", 0)),
                    "state": int(rec.get("state", 0)),
                    "mic_db10": [int(rec.get("r%d_10" % i, -900)) for i in range(4)],
                })
        self._last_seq = int(rec["seq"])
        self._parsed += 1
        self._records.append((now, int(rec["kind"]), int(rec["trust"]),
                              int(rec["spl10"])))
        if int(rec["kind"]) == 1:
            self._solve_pending += 1
            self._solve_total += 1

    def _doa_assess(self, now, last_angle):
        """多帧复核：判据全部与"阵列到底多大"解耦。

        为什么不再看绝对幅度：
          方位只由两路时延的【比值】决定（θ = atan2(Y, X)），
          阵列实际间距和标称值不一致时角度照样对，
          但 |τ|/τ标称 会整体缩小，旧判据于是把每一帧都判成"可疑"，
          页面上就永远只有"低置信/待确认"。

        现在用三层证据：
          · 帧间一致性：近 30 s 方位角的圆形离散度（主判据）
          · 帧内自洽  ：固件最小二乘残差（多对时延是否互相自洽）
          · 孔径稳定  ：实测孔径在窗口内是否稳定，而不是它等于多少
        """
        allf = [r for r in self._doa_records
                if now - float(r.get("ts", 0.0)) <= DOA_WINDOW_S and r.get("t02") is not None]
        # 只用固件自检通过的帧做聚类：没通过的帧要么六对时延互相矛盾，
        # 要么孔径接近 0（正对中垂线，方向本来就不可辨），
        # 把它们混进来只会让离散度永远降不下去。
        frames = [r for r in allf if r.get("trust")]
        if not frames:
            if allf:
                last = allf[-1]
                return {"state": "weak", "conf": 0, "frames": len(allf), "spread_deg": None,
                        "amp": None, "angle": last.get("angle_deg", last_angle),
                        "ap_ratio": float(last.get("ap") or 0) / 100.0,
                        "resid_pct": last.get("resid"), "mask": last.get("mask"),
                        "ok_ratio": 0.0}
            return {"state": "none", "conf": 0, "frames": 0, "spread_deg": None,
                    "amp": None, "angle": last_angle, "ap_ratio": None,
                    "resid_pct": None, "mask": None, "ok_ratio": 0.0}

        n = len(frames)
        sx = sum(math.cos(math.radians(r["angle_deg"])) for r in frames)
        sy = sum(math.sin(math.radians(r["angle_deg"])) for r in frames)
        result = min(1.0, math.hypot(sx, sy) / n)
        spread = math.degrees(math.acos(max(-1.0, min(1.0, result))))
        center = math.degrees(math.atan2(sy, sx))

        last = frames[-1]
        mask = int(last.get("mask") or 0xF)
        ok_ratio = sum(1 for r in frames if r.get("trust")) / float(n)
        resid_avg = sum(float(r.get("resid") or 0) for r in frames) / float(n)

        aps = [float(r.get("ap") or 0) for r in frames if float(r.get("ap") or 0) > 0]
        if aps:
            aps_sorted = sorted(aps)
            ap_med = aps_sorted[len(aps_sorted) // 2]
            dev = sorted(abs(a - ap_med) for a in aps)
            ap_mad = dev[len(dev) // 2]
            ap_stab = max(0.0, 1.0 - ap_mad / 12.0)
        else:
            ap_med, ap_mad, ap_stab = 0.0, 0.0, 0.0

        support = min(1.0, n / 4.0)
        agree = max(0.0, 1.0 - spread / 60.0)
        resid_term = max(0.0, 1.0 - resid_avg / 35.0)

        conf = int(round(100.0 * (0.30 * support + 0.40 * agree +
                                  0.15 * ok_ratio + 0.15 * ap_stab)))
        if spread > 45.0:
            conf = min(conf, 35)
        if n < 3:
            conf = min(conf, 40)

        confirmed = (n >= 3 and spread <= DOA_AGREE_DEG and ok_ratio >= 0.5)
        return {
            "state": "confirmed" if confirmed else "weak",
            "conf": conf,
            "frames": n,
            "spread_deg": round(spread, 1),
            "amp": round(math.hypot(last["t02"], last["t13"]) / TAU_DIAG, 2),
            "angle": round(center, 1) if confirmed else last_angle,
            "ap_ratio": round(ap_med / 100.0, 2),
            "ap_mad": round(ap_mad / 100.0, 2),
            "resid_pct": round(resid_avg, 1),
            "resid_term": round(resid_term, 2),
            "mask": mask,
            "ok_ratio": round(ok_ratio, 2),
        }
    def thresholds(self):
        # 页面保留兼容字段；真实门控值由每条 QZ 行的 thr10/floor10 携带。
        return {
            "spl_quiet": 400, "spl_warn": 500, "spl_alarm": 600,
            "conf_th": 60,
            "t_warn_to_alarm_ms": 4000,
            "t_active_gap_grace_ms": 600,
            "t_warn_release_ms": 2000,
            "t_alarm_release_ms": 3000,
        }

    def doa_events_since(self, since_ts=0.0):
        """返回尚未固化的新定位帧；只读复制，避免把队列对象暴露给调用方。"""
        with self._lock:
            return [dict(row) for row in self._doa_records if row.get("ts", 0) > since_ts]

    def read(self):
        now = time.time()
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            if self._last is None:
                raise TimeoutError("等待 sound_loc 的 QZ 遥测；请检查 UART 和 ELF 是否已运行")
            last = dict(self._last)
            self._records = deque(
                (r for r in self._records if now - r[0] <= 60.0),
                maxlen=self._records.maxlen,
            )
            recent10 = [r for r in self._records if now - r[0] <= 10.0]
            spls = [r[3] for r in self._records]
            spl10 = [r[3] for r in recent10]
            kinds = [r for r in recent10 if r[1] == 1]
            state = int(last["state"])
            state_name = ("IDLE", "QUIET", "WARN", "ALARM")[state]
            age = max(0.0, now - self._last_rx)
            fresh = age <= 2.0
            kind = int(last["kind"])
            trust = bool(int(last["trust"]) & 1) and kind == 1
            doa_last = self._last_doa
            doa_age = (None if doa_last is None
                       else max(0.0, now - self._last_doa_rx))
            doa_has = doa_last is not None
            doa_ok = doa_has and bool(doa_trust_bits(doa_last.get("trust", 0))["ok"])
            spl_now = int(last["spl10"])
            spl_avg10 = int(round(sum(spl10) / len(spl10))) if spl10 else spl_now
            spl_peak10 = max(spl10) if spl10 else spl_now
            spl_avg60 = int(round(sum(spls) / len(spls))) if spls else spl_now
            valid_ratio = int(round(sum(1 for r in kinds if r[2]) * 100.0 / max(1, len(recent10))))
            levels = [int(last.get("r%d_10" % i, -900)) for i in range(4)]
            angle10 = int((doa_last or {}).get("az10", 0))
            assess = self._doa_assess(now, (angle10 / 10.0) if doa_has else None)
            conf = 100 if trust else (45 if kind == 1 else 0)
            quiet_index = max(0, min(100, int(round((75.0 - spl_now / 10.0) * 4.0))))
            solve_cnt = self._solve_total
            self._solve_pending = 0
            events = list(self._events)
            self._events.clear()

            snap = {
                "_schema": "sound_loc_uart",
                "state": state,
                "state_name": state_name,
                "spl_now_db10": spl_now,
                "spl_avg_10s_db10": spl_avg10,
                "spl_peak_10s_db10": spl_peak10,
                "spl_avg_60s_db10": spl_avg60,
                "conf_now": conf,
                "speech_ratio": valid_ratio,
                "disturb_cnt": self._disturb_cnt,
                "occupy_sec": int(self._occupy_s),
                "quiet_index": quiet_index,
                "state_dwell_sec": int(max(0.0, now - self._state_since)),
                "ev_drop_cnt": 0,
                "mic_rms_db10": levels,
                "mic_dom_idx": max(range(4), key=lambda i: levels[i]),
                "mic_peak": levels,
                "doa_signed": True,
                "doa_angle_deg": angle10 / 10.0,
                "doa_has_value": doa_has,
                "doa_ok": doa_ok,
                "doa_fresh": fresh and kind == 1,
                "doa_age_sec": None if doa_age is None else round(doa_age, 2),
                "doa_seq": int((doa_last or {}).get("seq", 0)),
                "doa_delay_x": int((doa_last or {}).get("t02_100", 0)) / 100.0,
                "doa_delay_y": int((doa_last or {}).get("t13_100", 0)) / 100.0,
                "doa_peak_x": 0,
                "doa_peak_y": 0,
                "doa_solve_cnt": solve_cnt,
                "doa_t02": int((doa_last or {}).get("t02_100", 0)) / 100.0,
                "doa_t13": int((doa_last or {}).get("t13_100", 0)) / 100.0,
                "doa_state": assess["state"],
                "doa_conf": assess["conf"],
                "doa_frames": assess["frames"],
                "doa_spread_deg": assess["spread_deg"],
                "doa_amp": assess["amp"],
                "doa_cluster_angle_deg": assess["angle"],
                "doa_mic_mask": (assess["mask"] if assess["mask"] is not None
                                 else doa_trust_bits((self._last or {}).get("trust", 0))["mask"]),
                "doa_resid_pct": assess["resid_pct"],
                "doa_ap_ratio": assess["ap_ratio"],
                "doa_pairs": [list(p) for p in doa_pairs_for_mask(
                    doa_trust_bits((doa_last or {}).get("trust", 0))["mask"])],
                "doa_pair_0": "-".join(str(x) for x in doa_pairs_for_mask(
                    doa_trust_bits((doa_last or {}).get("trust", 0))["mask"])[0]) if doa_has else None,
                "doa_pair_1": ("-".join(str(x) for x in doa_pairs_for_mask(
                    doa_trust_bits((doa_last or {}).get("trust", 0))["mask"])[1])
                    if doa_has and len(doa_pairs_for_mask(
                        doa_trust_bits((doa_last or {}).get("trust", 0))["mask"])) > 1 else None),
                "doa_host_total": self._solve_total,
                "gate_thr_dbfs10": int(last.get("thr10", -550)),
                "gate_floor_dbfs10": int(last.get("floor10", -700)),
                "classifier_conf": int(last.get("clsconf", 0)),
                "classifier_effective_conf": int(last.get("clseff", 0)),
                "classifier_speech": int(bool(int(last.get("spklass", 0)))),
                "classifier_flatness_1000": int(last.get("flat1000", 0)),
                "classifier_centroid_hz": int(last.get("centroid_hz", 0)),
                "classifier_flux_1000": int(last.get("flux1000", 0)),
                "classifier_low_ratio_1000": int(last.get("low1000", 0)),
                "classifier_peak_ratio_1000": int(last.get("peak1000", 0)),
                "classifier_zcr_10": int(last.get("zcr10", 0)),
                "speech_accum_ms": int(last.get("speech_ms", 0)),
                "other_accum_ms": int(last.get("other_ms", 0)),
                "below_threshold_ms": int(last.get("below_ms", 0)),
                "uart_seq": int(last.get("seq", 0)),
                "uart_kind": kind,
                "uart_trust": int(bool(int(last.get("trust", 0)))),
                "uart_age_sec": round(age, 2),
                "uart_bad_total": int(last.get("bad", 0)),
                "uart_ring_wr": int(last.get("wr", 0)),
                "uart_ring_rd": int(last.get("rd", 0)),
                "uart_lines_total": self._parsed,
                "uart_lines_ignored": self._ignored,
                "uart_solve_total": self._solve_total,
            }
            return snap, events

    def is_paused(self):
        return False

    def control(self, action):
        return False, "sound_loc 的实时控制由板上固件维护；当前串口接口不支持 %s" % action

    def close(self):
        self._stop.set()
        try:
            self._rx.close()
        except Exception:
            pass


# ======================================================================
# 数据源 3：脚本化演示（不是真实测量）
# ======================================================================
class DemoSource:
    mode = "demo"
    label = "模拟演示"
    addr = "—"
    note = "脚本生成的会话时间线，用于在没有板卡时看页面与判读规则；数字不是真实测量"

    # 会话脚本：(持续秒, 状态, SPL 区间 dB(A), conf 区间, 是否有人)
    SCRIPT = [
        (30,  "IDLE",  (33.0, 36.0), (4, 16),   False),
        (100, "QUIET", (39.0, 45.0), (28, 55),  True),
        (50,  "WARN",  (53.0, 58.0), (68, 88),  True),
        (50,  "ALARM", (65.0, 72.0), (84, 96),  True),
        (35,  "WARN",  (52.0, 57.0), (66, 86),  True),
        (35,  "QUIET", (40.0, 46.0), (30, 52),  True),
    ]
    CYCLE = sum(s[0] for s in SCRIPT)

    EV_ON_CHANGE = {
        "QUIET": 1, "WARN": 3, "ALARM": 4,
    }

    def __init__(self, seed=7):
        self.rng = random.Random(seed)
        self.t0 = time.time()
        self.th = dict(hw_ifc.FACTORY_TH)
        self.win10, self.win60, self.sp10 = [], [], []
        self.peak = 0
        self.occupy = 0
        self.disturb = 0
        self.state = "IDLE"
        self.dwell = 0
        self.sec = -1
        self.quiet_index = 0
        self.cur_spl = 0.0
        self.cur_conf = 0
        self.paused = False
        self.tick_ts = self.t0

    # ---- 会话脚本：第 sec 秒落在哪一段 ----
    #   返回 (该段持续秒数, 状态, SPL 区间 dB(A), conf 区间, 是否有人)
    def _profile(self, sec):
        t = sec % self.CYCLE
        acc = 0
        for dur_s, state, spl, conf, occupied in self.SCRIPT:
            if t < acc + dur_s:
                return dur_s, state, spl, conf, occupied
            acc += dur_s
        return self.SCRIPT[-1]

    # ---- 1 秒推进一次，窗口算法刻意与 rtl/stat_acc.v 对齐 ----
    def _tick(self):
        nxt = self.sec + 1
        dur, state, (lo, hi), (clo, chi), occupied = self._profile(nxt)
        spl = self.rng.uniform(lo, hi)
        conf = self.rng.randint(clo, chi)

        prev, self.sec = self.state, nxt
        self.dwell = 0 if state != prev else self.dwell + 1
        self.state = state
        if occupied:
            self.occupy += 1
        if state == "ALARM" and state != prev:
            self.disturb += 1

        self.win10.append(spl)
        self.win60.append(spl)
        if len(self.win10) > 10:
            self.win10.pop(0)
        if len(self.win60) > 60:
            self.win60.pop(0)
        self.sp10.append(1 if conf >= DISPLAY_TH["conf_active"] else 0)
        if len(self.sp10) > 10:
            self.sp10.pop(0)

        # 慢衰减峰值保持，与 stat_acc.v 同口径：更高就顶上，否则每秒 1.0 dB 衰减
        if spl * 10.0 > self.peak:
            self.peak = spl * 10.0
        else:
            self.peak = max(0.0, self.peak - 10.0)

        quiet = self.th["spl_quiet"] / 10.0
        warn = self.th["spl_warn"] / 10.0
        if spl <= quiet:
            self.quiet_index = 100
        elif spl >= warn:
            self.quiet_index = int(max(0, 100 - (spl - quiet) * 8))
        else:
            self.quiet_index = int(100 - (spl - quiet) / max(0.1, warn - quiet) * 35)

        self.cur_spl, self.cur_conf = spl, conf
        return prev, state

    EV_TRANS = {
        ("IDLE", "QUIET"): (1, "occupy_on"),
        ("QUIET", "IDLE"): (2, "occupy_off"),
        ("WARN", "IDLE"): (2, "occupy_off"),
        ("ALARM", "IDLE"): (2, "occupy_off"),
        ("QUIET", "WARN"): (3, "to_warn"),
        ("IDLE", "WARN"): (3, "to_warn"),
        ("QUIET", "ALARM"): (4, "to_alarm_burst"),
        ("WARN", "ALARM"): (4, "to_alarm_burst"),
        ("WARN", "QUIET"): (6, "back_quiet"),
        ("ALARM", "QUIET"): (6, "back_quiet"),
        ("ALARM", "WARN"): (7, "back_warn"),
    }

    def thresholds(self):
        """演示模式没有 PL 可读，直接给出厂阈值；页面上会标明阈值来源。"""
        return dict(hw_ifc.FACTORY_TH)

    def read(self):
        now = time.time()
        if self.paused:                     # 暂停期间时间线不前进（与 PL「暂停统计」同口径）
            self.t0 += now - self.tick_ts
        self.tick_ts = now
        elapsed = int(now - self.t0)
        events = []
        guard = 0
        while self.sec < elapsed and guard < 100000:
            guard += 1
            prev, cur = self._tick()
            if cur != prev:
                code, name = self.EV_TRANS.get((prev, cur), (0, "none"))
                events.append({
                    "raw": 0, "code": code, "name": name,
                    "level_after": {"IDLE": 0, "QUIET": 1, "WARN": 2, "ALARM": 3}[cur],
                    "level_after_name": cur, "ts": now,
                })
        return self._snapshot(now), events

    def _snapshot(self, now):
        spl10 = self.cur_spl
        snap = {
            "state": {"IDLE": 0, "QUIET": 1, "WARN": 2, "ALARM": 3}[self.state],
            "state_name": self.state,
            "fft_busy": False,
            "lcd_busy": False,
            "led_busy": False,
            "ev_overflow": False,
            "spl_now_db10": int(round(self.cur_spl * 10)),
            "spl_now_db": round(self.cur_spl, 1),
            "conf_now": self.cur_conf,
            "spl_avg_10s_db10": int(round(sum(self.win10) / len(self.win10) * 10)) if self.win10 else 0,
            "spl_peak_10s_db10": int(round(self.peak)),
            "spl_avg_60s_db10": int(round(sum(self.win60) / len(self.win60) * 10)) if self.win60 else 0,
            "speech_ratio": int(round(sum(self.sp10) * 100.0 / len(self.sp10))) if self.sp10 else 0,
            "disturb_cnt": self.disturb,
            "occupy_sec": self.occupy,
            "quiet_index": max(0, min(100, self.quiet_index)),
            "state_dwell_sec": self.dwell,
            "ev_drop_cnt": 0,
        }
        # 四麦 / DOA 模拟（对应 0xC4–0xE4 与 0xE8–0xF8）：
        #   四路能量跟着当前声压走，麦2 略高（模拟声源稍偏 +X），方位角按 30 秒一圈缓慢转动，
        #   这样页面上的四麦条与方位罗盘会联动，便于核对"数字与统计量吻合"。
        base = max(1.0, self.cur_spl - 25.0) * 12.0
        rms = [int(min(0xFFFF, base * random.uniform(0.88, 1.12) * (1.10 if i == 1 else 1.0)))
               for i in range(4)]
        snap["mic_rms"] = rms
        snap["mic_peak"] = [int(min(0xFFFF, v * 2.6)) for v in rms]
        snap["mic_dom_idx"] = max(range(4), key=lambda i: rms[i])
        snap["mic_dom_flag"] = (max(rms) * 3 > 4 * (sum(rms) - max(rms)))
        ang = int((now * 12.0) % 360)
        snap["doa_angle_deg"] = ang
        snap["doa_has_value"] = True
        snap["doa_age_sec"] = 0.0
        snap["doa_seq"] = int(now * 45) & 0xFFFF
        snap["doa_ok"] = snap["spl_now_db10"] > 320
        snap["doa_fresh"] = True
        snap["doa_solve_cnt"] = 1 if random.random() < 0.9 else 0
        snap["doa_delay_x"] = round(6.0 * (1.0 - ang / 180.0), 3)
        snap["doa_delay_y"] = round(6.0 * (1.0 - abs(ang - 180) / 180.0), 3)
        snap["doa_peak_x"] = int(max(0, 1600 - abs(ang - 90) * 3))
        snap["doa_peak_y"] = int(max(0, 1600 - abs(ang - 270) * 3))
        return snap

    def is_paused(self):
        return self.paused

    def control(self, action):
        if action in ("pause", "resume", "clear", "soft_reset"):
            if action == "pause":
                self.paused = True
            elif action == "resume":
                self.paused = False
            if action == "clear":
                self.occupy = 0
                self.disturb = 0
                self.peak = 0
            return True, "演示模式下已响应 %s（仅影响本进程的模拟值）" % action
        return False, "未知操作 %s" % action

    def close(self):
        pass


# ======================================================================
# 判读规则：把统计量翻译成"管理人员需要注意的事"
#   每条规则都写清"依据哪个寄存器/阈值"，页面上直接照抄这句 detail。
# ======================================================================
class Attention:
    def __init__(self, th=None):
        self.th = dict(DISPLAY_TH)
        if th:
            self.th.update(th)
        self.since = {}
        self.low_run = 0         # 连续低电平样本（疑似麦克风断线）
        self.mic_imb_run = 0     # 四麦能量不均衡的连续样本数
        self.doa_bias_run = 0    # 方位长期偏一侧的连续样本数
        self.serial_imb_run = 0
        self.serial_untrusted_run = 0
        self.serial_silent_run = 0

    def _mk(self, rid, level, title, metric, detail, action, ts):
        if rid not in self.since:
            self.since[rid] = ts
        return {
            "id": rid, "level": level, "title": title, "metric": metric,
            "detail": detail, "action": action,
            "since_iso": iso(self.since[rid]),
            "since_sec": int(ts - self.since[rid]),
        }

    def _evaluate_serial(self, snap, ts):
        """面向 sound_loc UART 的判读：只使用串口实际提供的字段。"""
        out = []
        age = float(snap.get("uart_age_sec", 0.0))
        state = int(snap.get("state", 0))
        levels = [int(v) for v in snap.get("mic_rms_db10", [-900] * 4)]
        spread_db = (max(levels) - min(levels)) / 10.0
        if age > 2.0:
            out.append(self._mk("uart_stale", "critical", "串口遥测中断",
                "%.1f s 未收到 QZ 行" % age,
                "板卡仍在运行但网页已收不到 sound_loc 遥测。",
                "检查 CP210x 串口是否被其他终端占用、USB 线是否松动，并确认 ELF 正在运行。", ts))
        if state == 3:
            out.append(self._mk("state_alarm", "critical", "当前为干扰区",
                "%.1f dB(A)" % (snap.get("spl_now_db10", 0) / 10.0),
                "sound_loc 的响度门控将当前帧判为干扰区。",
                "优先核实声源并检查是否需要现场干预。", ts))
        elif state == 2:
            out.append(self._mk("state_warn", "warning", "当前为注意区",
                "%.1f dB(A)" % (snap.get("spl_now_db10", 0) / 10.0),
                "sound_loc 的响度门控将当前帧判为注意区。",
                "观察是否持续升级；单次短声可视为正常。", ts))
        if max(levels) > -750 and spread_db >= 12.0:
            self.serial_imb_run += 1
        else:
            self.serial_imb_run = 0
        if self.serial_imb_run >= 8:
            lo = min(range(4), key=lambda i: levels[i])
            hi = max(range(4), key=lambda i: levels[i])
            out.append(self._mk("serial_mic_imbalance", "warning", "四路麦克风能量差过大",
                "最大差 %.1f dB" % spread_db,
                "第%d 路 %.1f dBFS，第%d 路 %.1f dBFS。" %
                (lo + 1, levels[lo] / 10.0, hi + 1, levels[hi] / 10.0),
                "检查 CH%d 对应的 SD 线、100 kΩ 下拉和 INMP441 供电。" % (lo + 1), ts))
        if int(snap.get("uart_kind", 0)) == 1 and not bool(snap.get("uart_trust", 0)):
            self.serial_untrusted_run += 1
        else:
            self.serial_untrusted_run = 0
        if self.serial_untrusted_run >= 3:
            out.append(self._mk("serial_doa_untrusted", "warning", "方位解算可信度不足",
                "连续 %d 帧" % self.serial_untrusted_run,
                "固件的一致性检查把当前方位帧标为 suspect。",
                "查看 t02/t13 是否超出物理窗口；单次标黄可忽略，持续出现要检查四麦摆位与接线。", ts))
        if max(levels) <= -850:
            self.serial_silent_run += 1
        else:
            self.serial_silent_run = 0
        if self.serial_silent_run >= 8:
            out.append(self._mk("serial_mic_silent", "warning", "四路麦克风都没有有效信号",
                "最高 %.1f dBFS" % (max(levels) / 10.0),
                "连续多个采样点的四路电平都接近数字静音。",
                "检查四麦 3.3 V、CHIPEN、L/R 接地、SCK/WS/SD 以及 J10 新引脚映射。", ts))
        active = {a["id"] for a in out}
        for rid in list(self.since):
            if rid not in active:
                self.since.pop(rid, None)
        return out

    def evaluate(self, snap, th, st, ts):
        if snap.get("_schema") == "sound_loc_uart":
            return self._evaluate_serial(snap, ts)
        out = []
        thr = self.th
        spl_alarm = th["spl_alarm"] / 10.0
        spl_warn = th["spl_warn"] / 10.0
        spl_quiet = th["spl_quiet"] / 10.0

        # 0) 先判"传感链通不通"，再判"环境吵不吵"：
        #    四麦看 MIC_RMS1-4（0xC8–0xD4）与 MIC_DOM（0xC4），方位看 DOA_CNT（0xF8）与 DOA_ANGLE（0xE8）。
        #    这一组放在最前面，是因为它们出问题时后面所有声压数字都不成立。
        mic = _norm_mic(snap)
        doa = _norm_doa(snap)

        if mic is not None:
            if mic["imb_ratio"] >= thr["mic_imb_ratio"]:
                self.mic_imb_run += 1
            else:
                self.mic_imb_run = 0
            if self.mic_imb_run >= thr["mic_imb_samples"]:
                lo = min(range(4), key=lambda i: mic["rms_raw"][i])
                hi = max(range(4), key=lambda i: mic["rms_raw"][i])
                out.append(self._mk(
                    "mic_imbalance", "warning", "四路麦克风能量差别过大",
                    "第%d 路 %.2f / 第%d 路 %.2f（%.1f 倍）"
                    % (lo + 1, mic["rms_raw"][lo] / 256.0, hi + 1, mic["rms_raw"][hi] / 256.0,
                       mic["imb_ratio"]),
                    "连续 %.0f 秒四路窗能量最大最小差 %.1f 倍（判读阈值 %.1f 倍）。四颗麦装在同一平面、"
                    "增益一致时不该差这么多。" % (self.mic_imb_run * 0.5, mic["imb_ratio"],
                                                  thr["mic_imb_ratio"]),
                    "查这一路在 J10-8 / J10-10 上的 SD 线与 100 kΩ 下拉、麦克风 VDD / CHIPEN；"
                    "也可能是被遮挡或朝向装反。", ts))
            if max(mic["rms_raw"]) <= thr["mic_quiet_raw"]:
                out.append(self._mk(
                    "mic_all_quiet", "info", "四路麦克风都没有明显信号",
                    "MIC_RMS1-4 最大 %d" % max(mic["rms_raw"]),
                    "四路窗能量都不超过 %d（Q8.8 的 %.4f）。真实安静房间也不会四路同时这么低，"
                    % (thr["mic_quiet_raw"], thr["mic_quiet_raw"] / 256.0),
                    "先量 i2s_sck / i2s_ws 两根线，再量四颗麦的 VDD 与 CHIPEN。", ts))

        if doa is not None:
            ds = st.get("doa_stale_sec") or 0.0
            if ds > thr["doa_stale_sec"] and not st.get("paused"):
                out.append(self._mk(
                    "doa_stale", "warning", "方位解算没有在出结果",
                    "DOA_CNT 连续 %.0f 秒读到 0" % ds,
                    "DOA_CNT（0xF8）是读后清零的解算计数，PL 正常每 5.24 ms 解算一次，"
                    "页面每轮读都该看到非零。已经 %.0f 秒读到 0，0xE8 的方位角也不可信。" % ds,
                    "先看 MIC_RMS1-4 有没有样点在进；再看 DOA 复位后有没有重新启动。", ts))
            ang = doa["angle"]
            signed = ang if ang <= 180 else ang - 360
            if doa["ok"] and abs(signed) >= thr["doa_bias_deg"]:
                self.doa_bias_run += 1
            else:
                self.doa_bias_run = 0
            if self.doa_bias_run * 0.5 >= thr["doa_bias_sec"]:
                out.append(self._mk(
                    "doa_bias", "info", "声源长期集中在同一侧",
                    "方位角 %d 度（%d 侧，已持续 %.0f 秒）"
                    % (ang, 1 if signed >= 0 else -1, self.doa_bias_run * 0.5),
                    "DOA_ANGLE（0xE8）连续 %.0f 秒落在同一侧（判读阈值 %d 度）。"
                    % (self.doa_bias_run * 0.5, thr["doa_bias_deg"]),
                    "若那是固定噪声源（空调 / 门口 / 打印机），这条就是排布优化依据；"
                    "若该方向本来不该有声音，核对四麦朝向与 +(X) 轴约定。", ts))

        # 1) 事件队列溢出：数据可能缺失，管理上必须先知道
        if snap["ev_overflow"]:
            out.append(self._mk("ev_overflow", "critical", "事件队列溢出",
                                "STATUS.bit5 = 1",
                                "PL 的事件 FIFO 溢出过，EVENT_FIFO 里的事件有丢失，统计口径不再完整。",
                                "先清统计并观察是否复现；若持续出现说明 PS 轮询太慢。", ts))

        # 2) 统计暂停
        if st.get("paused"):
            out.append(self._mk("paused", "info", "统计累计已暂停",
                                "CTRL.STAT_PAUSE = 1",
                                "10 s / 60 s 窗口与占用时长都不再更新，页面数字会停在暂停那一刻。",
                                "确认是谁暂停的；不需要时在页面上点『继续统计』。", ts))

        # 3) 麦克风疑似断线（先于声压判读，否则会把静音当成"安静"）
        if snap["spl_now_db10"] <= thr["mic_dead_db10"] and snap["conf_now"] == 0:
            self.low_run += 1
        else:
            self.low_run = 0
        if self.low_run >= thr["mic_dead_samples"]:
            out.append(self._mk("mic_dead", "warning", "麦克风疑似没有接上",
                                "SPL_NOW = %.1f dB(A)" % (snap["spl_now_db10"] / 10.0),
                                "连续 %d 拍（约 %.0f 秒）读数为 %.1f dB(A) 且置信度恒为 0，"
                                "更像 SD 线没接/麦没供电，而不是环境真的安静。"
                                % (self.low_run, thr["mic_dead_samples"] * 0.5, snap["spl_now_db10"] / 10.0),
                                "查 J10-8 / J10-10 的 SD 线、L/R 与 VDD，以及 100 kΩ 下拉。", ts))

        # 4) 统计停摆：STATE_DWELL 每秒都该变，长时间不动说明 PL 侧没在更新
        if (st.get("dwell_frozen_sec") or 0) > thr["dwell_stale_sec"] and not st.get("paused"):
            out.append(self._mk("dwell_frozen", "critical", "统计量已停止更新",
                                "STATE_DWELL %.0f 秒没变" % st["dwell_frozen_sec"],
                                "STATE_DWELL（0x2C）是每秒递增的计秒器，已经 %.0f 秒没有变化，"
                                "说明 PL 侧统计链卡住或复位后没有再启动，页面上的其它数字都不可信。"
                                % st["dwell_frozen_sec"],
                                "先看板卡是否在跑、位流是否加载；必要时在页面上点『软复位』。", ts))
        else:
            self.since.pop("dwell_frozen", None)

        # 5) 当前声压
        if snap["spl_now_db10"] >= th["spl_alarm"]:
            out.append(self._mk("spl_alarm", "critical", "当前已超过吵闹门限",
                                "%.1f dB(A) ≥ %.1f" % (snap["spl_now_db10"] / 10.0, spl_alarm),
                                "SPL_NOW 越过 TH_ALARM，状态机已经把本工位判成 ALARM。",
                                "现场确认声源；需要时可人工介入提醒。", ts))
        elif snap["spl_now_db10"] >= th["spl_warn"]:
            out.append(self._mk("spl_warn", "warning", "当前超过警戒门限",
                                "%.1f dB(A) ≥ %.1f" % (snap["spl_now_db10"] / 10.0, spl_warn),
                                "SPL_NOW 越过 TH_WARN，属于「偏吵」区间。",
                                "观察是否继续升高；持续则会被判为 ALARM。", ts))
        else:
            self.since.pop("spl_warn", None)
            self.since.pop("spl_alarm", None)

        # 6) 10 秒均值越线
        if snap["spl_avg_10s_db10"] >= th["spl_warn"]:
            out.append(self._mk("avg10", "warning", "10 秒平均处于偏吵水平",
                                "%.1f dB(A)（10 s 均值）" % (snap["spl_avg_10s_db10"] / 10.0),
                                "SPL_AVG_10S 是 §4.8 的真滑窗均值，不受瞬时尖峰影响。",
                                "按「不是偶发噪声」处理。", ts))
        else:
            self.since.pop("avg10", None)

        # 7) 持续交谈
        if snap["speech_ratio"] >= thr["speech_ratio_high"]:
            out.append(self._mk("speech_high", "warning", "持续交谈",
                                "人声占比 %d%%" % snap["speech_ratio"],
                                "SPEECH_RATIO 是最近 10 秒里判为人声的帧占比（0-100）。",
                                "属于「人说话」而不是「环境噪声」，提醒更有效。", ts))
        elif snap["speech_ratio"] >= thr["speech_ratio_mid"]:
            out.append(self._mk("speech_mid", "info", "人声占比偏高",
                                "%d%%" % snap["speech_ratio"],
                                "最近 10 秒有一半左右的时间判到人声。",
                                "继续观察即可。", ts))
        else:
            self.since.pop("speech_high", None)
            self.since.pop("speech_mid", None)

        # 8) 干扰频发（近 10 分钟）
        if st.get("disturb_recent", 0) >= thr["disturb_burst"]:
            out.append(self._mk("disturb_burst", "warning", "干扰事件频发",
                                "近 10 分钟 %d 次" % st["disturb_recent"],
                                "当日 DISTURB_CNT 累计 %d 次，其中近 10 分钟新增 %d 次。"
                                % (snap["disturb_cnt"], st["disturb_recent"]),
                                "看事件时间轴确认集中在哪个时段，必要时调整工位。", ts))
        else:
            self.since.pop("disturb_burst", None)

        # 9) 长时间占座且一直有声
        if snap["occupy_sec"] >= thr["occupy_long_sec"] and snap["spl_avg_60s_db10"] >= th["spl_quiet"]:
            out.append(self._mk("occupy_noisy", "warning", "长时间占座且持续出声",
                                "占座 %.0f 分钟 / 60 s 均值 %.1f dB(A)"
                                % (snap["occupy_sec"] / 60.0, snap["spl_avg_60s_db10"] / 10.0),
                                "OCCUPY_SEC 累计 %.0f 分钟，同时 SPL_AVG_60S 仍高于安静门限 %.1f dB(A)。"
                                % (snap["occupy_sec"] / 60.0, spl_quiet),
                                "这类时段是「占座又吵闹」，优先安排巡查。", ts))
        else:
            self.since.pop("occupy_noisy", None)

        # 10) 空置
        if snap["state"] == 0 and snap["state_dwell_sec"] >= 300:
            out.append(self._mk("vacant", "info", "工位长时间空置",
                                "IDLE 已 %d 秒" % snap["state_dwell_sec"],
                                "STATE_DWELL 显示 IDLE 已持续 %d 秒（PIR 连续 60 s 无人）。"
                                % snap["state_dwell_sec"],
                                "无需处理，用于解释「为什么没有数据」。", ts))
        else:
            self.since.pop("vacant", None)

        for rid in list(self.since):
            if rid not in [o["id"] for o in out]:
                self.since.pop(rid, None)

        if not out:
            self.since.pop("all_ok", None)
            out.append(self._mk("all_ok", "ok", "本时段无需关注",
                                "状态 %s / %.1f dB(A)" % (snap["state_name"], snap["spl_now_db10"] / 10.0),
                                "没有规则被触发：声压在人声/噪声门限以下，人声占比不高，干扰计数没有快速增长。",
                                "保持现状即可。", ts))
            self.since.pop("all_ok", None)
            out[-1]["since_sec"] = 0

        order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
        out.sort(key=lambda x: (order.get(x["level"], 9), -x["since_sec"]))
        return out


# ======================================================================
# 采样与统计汇总
# ======================================================================
REG_TABLE = [
    ("CTRL",          "0x00", "控制：软复位 / 清统计 / 暂停统计"),
    ("STATUS",        "0x04", "状态：状态机状态 + 三条忙标志 + 事件溢出"),
    ("SPL_NOW",       "0x08", "当前帧声压级 dB(A)×10"),
    ("CONF_NOW",      "0x0C", "当前帧人声置信度 0-255"),
    ("SPL_AVG_10S",   "0x10", "最近 10 s 声压级均值 dB(A)×10"),
    ("SPL_PEAK_10S",  "0x14", "10 s 慢衰减峰值 dB(A)×10"),
    ("SPL_AVG_60S",   "0x18", "最近 60 s 声压级均值 dB(A)×10"),
    ("SPEECH_RATIO",  "0x1C", "10 s 内判为人声的帧占比 0-100"),
    ("DISTURB_CNT",   "0x20", "当日干扰事件计数"),
    ("OCCUPY_SEC",    "0x24", "当日有人时长（秒）"),
    ("QUIET_INDEX",   "0x28", "安静指数 0-100"),
    ("STATE_DWELL",   "0x2C", "当前状态已持续秒数"),
    ("EV_DROP_CNT",   "0x34", "被丢弃的事件数"),
    # ---- 四麦遥测区（0xC4–0xE4，040 轮）----
    ("MIC_DOM",       "0xC4", "最强声道编号（bit1:0）+ 优势标志（bit2）"),
    ("MIC_RMS1",      "0xC8", "麦1 窗能量 Q8.8（四路同口径）"),
    ("MIC_RMS2",      "0xCC", "麦2 窗能量 Q8.8"),
    ("MIC_RMS3",      "0xD0", "麦3 窗能量 Q8.8"),
    ("MIC_RMS4",      "0xD4", "麦4 窗能量 Q8.8"),
    ("MIC_PK1",       "0xD8", "麦1 窗内峰值（原始码值）"),
    ("MIC_PK2",       "0xDC", "麦2 窗内峰值"),
    ("MIC_PK3",       "0xE0", "麦3 窗内峰值"),
    ("MIC_PK4",       "0xE4", "麦4 窗内峰值"),
    # ---- DOA 只读遥测区（0xE8–0xF8，048 轮；050 轮起为真实解算结果）----
    ("DOA_ANGLE",     "0xE8", "方位角（bit8:0 整数度）+ 有效（bit9）+ 新鲜（bit10）"),
    ("DOA_DELAY_X",   "0xEC", "X 轴时延 Q8.8（采样点，有符号）"),
    ("DOA_DELAY_Y",   "0xF0", "Y 轴时延 Q8.8（采样点，有符号）"),
    ("DOA_PEAK",      "0xF4", "互相关峰：[31:16] Y 轴 / [15:0] X 轴"),
    ("DOA_CNT",       "0xF8", "方位解算次数（读后清零）"),
]


class App:
    def __init__(self, source, poll_s=0.5, maxlen=7200, stale_sec=30):
        self.src = source
        self.poll_s = poll_s
        self.stale_sec = stale_sec
        self.lock = threading.RLock()
        self.t0 = time.time()
        self.hist = []
        self.maxlen = maxlen
        self.events = []
        self.att = Attention()
        self.th = dict(hw_ifc.FACTORY_TH)
        self.th_source = "factory_default"   # pl = 从 PL 回读；factory_default = 读不到时退回出厂值
        self.th_read_ts = 0
        self.snap = {}
        self.stop_flag = False
        self.samples = 0
        self.state_sec = {"IDLE": 0.0, "QUIET": 0.0, "WARN": 0.0, "ALARM": 0.0}
        self.alarm_run = 0.0
        self.alarm_run_max = 0.0
        self.peak_spl_db = 0.0
        self.disturb_marks = []
        self.minutes = {}
        self.last_ts = None
        self.last_state = None
        self.last_dwell = None
        self.log_path = None
        self.history_dir = None
        self.history_error = None
        self.last_log_ts = 0.0
        self.last_doa_event_ts = 0.0
        self.dwell_changed_ts = time.time()
        self.error = None
        self.last_attention = []             # 首次采样前 /api/state 也要能返回正常结构
        self.paused = False                  # 页面要用它显示 CTRL 的暂停状态

    # ---- 采样线程 ----
    def run(self):
        while not self.stop_flag:
            try:
                snap, evs = self.src.read()
                self._ingest(snap, evs)
                self.error = None
            except Exception as exc:            # 板卡掉了/权限不足/SIGBUS
                with self.lock:
                    self.error = "%s: %s" % (type(exc).__name__, exc)
            time.sleep(self.poll_s)

    def _ingest(self, snap, evs):
        now = time.time()
        with self.lock:
            if now - self.th_read_ts > 5.0:
                try:                             # 先铺出厂值，再用 PL 回读值覆盖
                    self.th = dict(hw_ifc.FACTORY_TH)
                    self.th.update(self.src.thresholds() or {})
                    self.th_source = "pl" if self.src.mode == "board" else ("sound_loc" if self.src.mode == "serial" else "demo")
                except Exception:
                    self.th = dict(hw_ifc.FACTORY_TH)
                    self.th_source = "factory_default"
                self.th_read_ts = now

            try:
                paused = self.src.is_paused()
            except Exception:
                paused = False
            self.paused = paused

            dt = 0.0 if self.last_ts is None else max(0.0, min(5.0, now - self.last_ts))
            if self.last_state is not None:
                self.state_sec[self.last_state] = self.state_sec.get(self.last_state, 0.0) + dt
            if snap["state_name"] == "ALARM":
                self.alarm_run += dt
                self.alarm_run_max = max(self.alarm_run_max, self.alarm_run)
            else:
                self.alarm_run = 0.0

            self.peak_spl_db = max(self.peak_spl_db, snap["spl_now_db10"] / 10.0)

            if self.last_dwell is None or snap["state_dwell_sec"] != self.last_dwell:
                self.last_dwell = snap["state_dwell_sec"]
                self.dwell_changed_ts = now
            dwell_frozen = now - self.dwell_changed_ts

            # 干扰计数增长（近 10 分钟）
            self.disturb_marks.append((now, snap["disturb_cnt"]))
            while self.disturb_marks and now - self.disturb_marks[0][0] > 600:
                self.disturb_marks.pop(0)
            recent = snap["disturb_cnt"] - self.disturb_marks[0][1] if self.disturb_marks else 0

            for e in evs:
                e = dict(e)
                e["ts"] = e.get("ts", now)
                e["iso"] = iso(e["ts"])
                text, level = EV_TEXT.get(e.get("name", "none"), ("未知事件", "info"))
                e["text"] = text
                e["level"] = level
                self.events.insert(0, e)
            del self.events[400:]

            mic = _norm_mic(snap)
            doa = _norm_doa(snap)
            # DOA 解算停摆跟踪：DOA_CNT 是读后清零的，PL 正常每 5.24 ms 解算一次，
            # 页面每轮读必然看到非零；连续读到 0 说明解算链停了。
            last_change = getattr(self, "doa_last_change_ts", None)
            if doa is not None and doa["solve_cnt"] > 0:
                last_change = now
                self.doa_last_change_ts = now
            doa_stale = 0.0 if last_change is None else max(0.0, now - last_change)

            ctx = {"paused": paused, "disturb_recent": recent, "dwell_frozen_sec": dwell_frozen,
                   "doa_stale_sec": doa_stale}
            attention = self.att.evaluate(snap, self.th, ctx, now)

            self.snap = dict(snap)
            self.samples += 1
            rec = {
                "ts": now, "iso": iso(now), "state": snap["state"], "state_name": snap["state_name"],
                "spl_now_db": round(snap["spl_now_db10"] / 10.0, 1),
                "spl_avg_10s_db": round(snap["spl_avg_10s_db10"] / 10.0, 1),
                "spl_peak_10s_db": round(snap["spl_peak_10s_db10"] / 10.0, 1),
                "spl_avg_60s_db": round(snap["spl_avg_60s_db10"] / 10.0, 1),
                "conf_now": snap["conf_now"], "speech_ratio": snap["speech_ratio"],
                "disturb_cnt": snap["disturb_cnt"], "occupy_sec": snap["occupy_sec"],
                "quiet_index": snap["quiet_index"], "state_dwell_sec": snap["state_dwell_sec"],
                # 四麦 / DOA：对应 0xC4–0xE4 与 0xE8–0xF8；数据源没给时为 None（页面显示"—"）
                "mic_rms1": None if mic is None else mic["rms_raw"][0],
                "mic_rms2": None if mic is None else mic["rms_raw"][1],
                "mic_rms3": None if mic is None else mic["rms_raw"][2],
                "mic_rms4": None if mic is None else mic["rms_raw"][3],
                "doa_angle": None if doa is None else doa["angle"],
                "doa_ok": None if doa is None else int(doa["ok"]),
                "doa_fresh": None if doa is None else int(doa["fresh"]),
                "doa_seq": None if doa is None else int(doa["seq"]),
                "classifier_conf": snap["classifier_conf"],
                "classifier_effective_conf": snap["classifier_effective_conf"],
                "classifier_speech": snap["classifier_speech"],
                "classifier_flatness_1000": snap["classifier_flatness_1000"],
                "classifier_centroid_hz": snap["classifier_centroid_hz"],
                "classifier_flux_1000": snap["classifier_flux_1000"],
                "classifier_low_ratio_1000": snap["classifier_low_ratio_1000"],
                "classifier_peak_ratio_1000": snap["classifier_peak_ratio_1000"],
                "classifier_zcr_10": snap["classifier_zcr_10"],
                "speech_accum_ms": snap["speech_accum_ms"],
                "other_accum_ms": snap["other_accum_ms"],
                "below_threshold_ms": snap["below_threshold_ms"],
            }
            self.hist.append(rec)
            if len(self.hist) > self.maxlen:
                del self.hist[0:len(self.hist) - self.maxlen]

            # 方位事件单独固化：主日志每 10 s 一条会漏掉两次日志之间的定位帧。
            self._persist_doa_events()

            # 分钟级聚合（管理视角：哪一段时间最吵）
            mi = int(now // 60)
            b = self.minutes.setdefault(mi, {"sum": 0.0, "n": 0, "max": 0.0, "loud": 0})
            b["sum"] += rec["spl_now_db"]
            b["n"] += 1
            b["max"] = max(b["max"], rec["spl_now_db"])
            if snap["state"] >= 2:
                b["loud"] += 1
            if len(self.minutes) > 180:
                for k in sorted(self.minutes)[:-180]:
                    self.minutes.pop(k, None)

            self.last_ts, self.last_state, self.last_attention = now, snap["state_name"], attention

            # 落盘：每 10 秒一行 JSONL，给"平台上报"、日历复盘和事后导出用
            if (self.log_path or self.history_dir) and now - self.last_log_ts >= 10.0:
                self.last_log_ts = now
                try:
                    payload = dict(rec)
                    payload.update({
                        "source": self.src.mode, "snapshot": snap,
                        "attention": [a["id"] for a in attention if a["level"] != "ok"],
                    })
                    line = json.dumps(payload, ensure_ascii=False) + "\n"
                    targets = []
                    if self.history_dir:
                        targets.append(os.path.join(
                            self.history_dir, "qz-%s.jsonl" % time.strftime("%Y-%m-%d", time.localtime(now))))
                    if self.log_path:
                        targets.append(self.log_path)
                    written = set()
                    for path in targets:
                        full_path = os.path.abspath(path)
                        if full_path in written:
                            continue
                        parent = os.path.dirname(full_path)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        with io.open(full_path, "a", encoding="utf-8", newline="\n") as fh:
                            fh.write(line)
                        written.add(full_path)
                    self.history_error = None
                except Exception as exc:
                    self.history_error = "%s: %s" % (type(exc).__name__, exc)

    def _persist_doa_events(self):
        if not self.history_dir:
            return
        getter = getattr(self.src, "doa_events_since", None)
        if not callable(getter):
            return
        try:
            events = getter(self.last_doa_event_ts)
            if not events:
                return
            grouped = {}
            new_last = self.last_doa_event_ts
            for event in events:
                event_ts = float(event.get("ts", 0))
                new_last = max(new_last, event_ts)
                if not event.get("trust"):
                    continue
                date_text = str(event.get("iso", time.strftime(
                    "%Y-%m-%d", time.localtime(event_ts))))[:10]
                grouped.setdefault(date_text, []).append(event)
            for date_text, rows in grouped.items():
                path = os.path.join(self.history_dir, "qz-doa-%s.jsonl" % date_text)
                os.makedirs(self.history_dir, exist_ok=True)
                with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
                    for event in rows:
                        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.last_doa_event_ts = new_last
            self.history_error = None
        except Exception as exc:
            self.history_error = "%s: %s" % (type(exc).__name__, exc)

    def _history_file(self, date_text):
        if not self.history_dir:
            return None
        return os.path.join(self.history_dir, "qz-%s.jsonl" % date_text)

    def _available_history_dates(self):
        dates = set()
        if self.history_dir and os.path.isdir(self.history_dir):
            for name in os.listdir(self.history_dir):
                if len(name) == 19 and name.startswith("qz-") and name.endswith(".jsonl"):
                    text = name[3:13]
                    try:
                        dt.datetime.strptime(text, "%Y-%m-%d")
                        dates.add(text)
                    except ValueError:
                        continue
        with self.lock:
            for rec in self.hist:
                text = str(rec.get("iso", ""))[:10]
                if text:
                    dates.add(text)
        return sorted(dates)

    def _calendar_records(self, date_text):
        records = []
        path = self._history_file(date_text)
        if path and os.path.isfile(path):
            with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        item = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if str(item.get("iso", ""))[:10] == date_text and item.get("spl_now_db") is not None:
                        records.append(item)
        with self.lock:
            records.extend(dict(rec) for rec in self.hist
                           if str(rec.get("iso", ""))[:10] == date_text)

        deduplicated = []
        seen = set()
        for rec in records:
            if rec.get("ts") is not None:
                key = round(float(rec["ts"]), 3)
            else:
                key = (rec.get("iso"), rec.get("state_name"), rec.get("spl_now_db"))
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(rec)
        return deduplicated

    def _calendar_doa_events(self, date_text, records):
        persisted = []
        event_file = None
        if self.history_dir:
            event_file = os.path.join(self.history_dir, "qz-doa-%s.jsonl" % date_text)
        if event_file and os.path.isfile(event_file):
            with io.open(event_file, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if str(event.get("iso", ""))[:10] == date_text and event.get("trust"):
                        persisted.append(event)

        first_persisted = min((str(event.get("iso", "")) for event in persisted), default=None)
        fallback = []
        last_seq = None
        for rec in sorted(records, key=lambda item: str(item.get("iso", ""))):
            if first_persisted and str(rec.get("iso", "")) >= first_persisted:
                continue
            snapshot = rec.get("snapshot") or {}
            if not (rec.get("doa_ok") or snapshot.get("doa_ok")):
                continue
            angle = rec.get("doa_angle", snapshot.get("doa_angle_deg"))
            seq = rec.get("doa_seq", snapshot.get("doa_seq"))
            fresh = rec.get("doa_fresh", snapshot.get("doa_fresh"))
            if seq:
                if seq == last_seq:
                    continue
                last_seq = seq
            elif not fresh:
                continue
            fallback.append({
                "ts": rec.get("ts", 0), "iso": rec.get("iso"),
                "angle_deg": angle, "trust": 1,
            })

        deduplicated = []
        seen = set()
        for event in persisted + fallback:
            try:
                key = (round(float(event.get("ts", 0)), 3), round(float(event.get("angle_deg")), 3))
            except (TypeError, ValueError):
                continue
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(event)
        return deduplicated, ("event_log" if persisted else "main_log_sequences")

    def calendar_payload(self, date_text=None):
        date_text = date_text or time.strftime("%Y-%m-%d")
        try:
            dt.datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            raise ValueError("日期必须为 YYYY-MM-DD")
        records = self._calendar_records(date_text)
        doa_events, doa_source = self._calendar_doa_events(date_text, records)
        result = aggregate_calendar(date_text, records, bin_minutes=15)
        result.update({
            "ok": True,
            "doa": aggregate_doa(doa_events, sector_degrees=10),
            "doa_source": doa_source,
            "available_dates": self._available_history_dates(),
            "history_enabled": bool(self.history_dir),
            "history_dir": self.history_dir,
            "history_error": self.history_error,
        })
        return result

    # ---- 四麦方位标定历史 ----
    def doa_history_payload(self, since=0.0, limit=5000):
        with self.lock:
            src = self.src
            records = getattr(src, "_doa_records", ())
            rows = [dict(r) for r in records if r["ts"] >= since]
            if limit > 0:
                rows = rows[-limit:]
            return {"ok": True, "count": len(rows), "records": rows}

    # ---- 对外的状态快照 ----
    def state_payload(self, window_s=900, max_points=240):
        with self.lock:
            now = time.time()
            hist = [r for r in self.hist if now - r["ts"] <= window_s]
            step = max(1, len(hist) // max_points)
            hist = hist[::step]
            total = sum(self.state_sec.values()) or 1e-9
            share = {k: round(v * 100.0 / total, 1) for k, v in self.state_sec.items()}
            occupy = self.snap.get("occupy_sec", 0)
            mins = []
            for k in sorted(self.minutes):
                b = self.minutes[k]
                mins.append({
                    "t": time.strftime("%H:%M", time.localtime(k * 60)),
                    "avg": round(b["sum"] / b["n"], 1),
                    "max": round(b["max"], 1),
                    "loud_pct": round(b["loud"] * 100.0 / b["n"], 0),
                })
            loudest = max(mins, key=lambda m: m["avg"]) if mins else None
            regs = []
            raw = self.snap
            mic = _norm_mic(raw)
            doa = _norm_doa(raw)
            # 寄存器表按 REG_TABLE 的顺序整表输出（含四麦 0xC4–0xE4 与 DOA 0xE8–0xF8），
            # 每个数字都标来源寄存器；四麦/DOA 那几行的值来自列表字段。
            key_of = {
                "SPL_NOW": "spl_now_db10", "CONF_NOW": "conf_now",
                "SPL_AVG_10S": "spl_avg_10s_db10", "SPL_PEAK_10S": "spl_peak_10s_db10",
                "SPL_AVG_60S": "spl_avg_60s_db10", "SPEECH_RATIO": "speech_ratio",
                "DISTURB_CNT": "disturb_cnt", "OCCUPY_SEC": "occupy_sec",
                "QUIET_INDEX": "quiet_index", "STATE_DWELL": "state_dwell_sec",
                "EV_DROP_CNT": "ev_drop_cnt",
            }
            ext = {}
            if mic:
                ext["MIC_DOM"] = mic["dom"]
                for i in range(4):
                    ext["MIC_RMS%d" % (i + 1)] = mic["rms_raw"][i]
                    ext["MIC_PK%d" % (i + 1)] = mic["peak"][i]
            if doa:
                ext["DOA_ANGLE"] = doa["angle"]
                ext["DOA_DELAY_X"] = doa["delay_x"]
                ext["DOA_DELAY_Y"] = doa["delay_y"]
                ext["DOA_PEAK"] = doa["peak_x"]
                ext["DOA_CNT"] = doa["solve_cnt"]
            if raw.get("_schema") == "sound_loc_uart":
                regs = [
                    {"name": "QZ_SEQ", "off": "UART", "value": raw.get("uart_seq"),
                     "desc": "sound_loc 帧号"},
                    {"name": "AZ10", "off": "UART", "value": round(raw.get("doa_angle_deg", 0) * 10),
                     "desc": "方位角×10，范围 -1800…1800"},
                    {"name": "SPL10", "off": "UART", "value": raw.get("spl_now_db10"),
                     "desc": "A 计权显示声压级×10，dB(A)"},
                    {"name": "STATE", "off": "UART", "value": raw.get("state"),
                     "desc": "0 待机 / 1 安静 / 2 注意 / 3 干扰"},
                    {"name": "TRUST", "off": "UART", "value": raw.get("uart_trust"),
                     "desc": "bit0 自检通过 / [7:4] 麦克风掩码 / [15:8] 残差% / [23:16] 孔径比%"},
                    {"name": "T02_100", "off": "UART", "value": round(raw.get("doa_t02", 0) * 100),
                     "desc": "第一对时延×100 采样点（通常是 0-2）"},
                    {"name": "T13_100", "off": "UART", "value": round(raw.get("doa_t13", 0) * 100),
                     "desc": "第二对时延×100 采样点（通常是 1-3，CH1 坏时为 0-3）"},
                    {"name": "THR10", "off": "UART", "value": raw.get("gate_thr_dbfs10"),
                     "desc": "固件本帧门限×10，dBFS"},
                    {"name": "WR_RD", "off": "GPIO", "value": "%s / %s" % (
                        raw.get("uart_ring_wr"), raw.get("uart_ring_rd")),
                     "desc": "AXI GPIO 乒乓写/读计数"},
                ]
            else:
                for name, off, desc in REG_TABLE:
                    if name in ext:
                        val = ext[name]
                    else:
                        key = key_of.get(name)
                        val = "—" if key is None else raw.get(key, "—")
                    regs.append({"name": name, "off": off, "value": val, "desc": desc})
            return {
                "ok": self.error is None,
                "error": self.error,
                "server": {
                    "now": now, "iso": iso(now), "uptime_s": int(now - self.t0),
                    "poll_s": self.poll_s, "samples": self.samples,
                    "paused": self.paused,
                    "source": {"mode": self.src.mode, "label": self.src.label,
                               "addr": self.src.addr, "note": self.src.note},
                },
                "snapshot": self.snap,
                "mic": mic,
                "doa": doa,
                "thresholds": self.th,
                "thresholds_source": self.th_source,
                "display_thresholds": self.att.th,
                "attention": self.last_attention,
                "stats": {
                    "session_s": int(now - self.t0),
                    "occupy_sec": occupy,
                    "occupy_ratio": round(occupy * 100.0 / max(1.0, now - self.t0), 1),
                    "state_share": share,
                    "pct_quiet_or_better": round(share.get("IDLE", 0) + share.get("QUIET", 0), 1),
                    "disturb_cnt": self.snap.get("disturb_cnt", 0),
                    "longest_alarm_s": int(self.alarm_run_max),
                    "peak_spl_db": round(self.peak_spl_db, 1),
                    "loudest_minute": loudest,
                    "alarm_minutes": sum(1 for m in mins if m["loud_pct"] >= 50),
                },
                "series": {
                    "t": [r["iso"][11:] for r in hist],
                    "spl_now": [r["spl_now_db"] for r in hist],
                    "spl_avg_10s": [r["spl_avg_10s_db"] for r in hist],
                    "spl_avg_60s": [r["spl_avg_60s_db"] for r in hist],
                    "speech_ratio": [r["speech_ratio"] for r in hist],
                    "conf": [r["conf_now"] for r in hist],
                    "quiet_index": [r["quiet_index"] for r in hist],
                    "mic_rms1": [r.get("mic_rms1") for r in hist],
                    "mic_rms2": [r.get("mic_rms2") for r in hist],
                    "mic_rms3": [r.get("mic_rms3") for r in hist],
                    "mic_rms4": [r.get("mic_rms4") for r in hist],
                    "doa_angle": [r.get("doa_angle") for r in hist],
                    "minutes": mins,
                },
                "events": list(self.events[:60]),
                "registers": regs,
                "state_color": STATE_COLOR,
            }

    def csv_text(self):
        with self.lock:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["time", "iso", "state_name", "spl_now_dBA", "spl_avg_10s_dBA",
                        "spl_peak_10s_dBA", "spl_avg_60s_dBA", "conf_now", "speech_ratio_pct",
                        "disturb_cnt", "occupy_sec", "quiet_index", "state_dwell_sec",
                        "mic_rms1", "mic_rms2", "mic_rms3", "mic_rms4", "doa_angle", "doa_ok"])
            for r in self.hist:
                w.writerow([r["ts"], r["iso"], r["state_name"], r["spl_now_db"],
                            r["spl_avg_10s_db"], r["spl_peak_10s_db"], r["spl_avg_60s_db"],
                            r["conf_now"], r["speech_ratio"], r["disturb_cnt"],
                            r["occupy_sec"], r["quiet_index"], r["state_dwell_sec"],
                            r.get("mic_rms1"), r.get("mic_rms2"), r.get("mic_rms3"),
                            r.get("mic_rms4"), r.get("doa_angle"), r.get("doa_ok")])
            return buf.getvalue()

    def control(self, action):
        ok, msg = self.src.control(action)
        if ok and action == "clear":
            with self.lock:
                self.state_sec = {k: 0.0 for k in self.state_sec}
                self.alarm_run = self.alarm_run_max = 0.0
                self.peak_spl_db = 0.0
                self.disturb_marks = []
        return ok, msg


# ======================================================================
# HTTP 层：静态页面 + JSON 状态 + CSV 导出 + 控制命令
#   只用标准库，PS 上不用装任何东西；chart.js 是本地文件，
#   板卡不联网也能打开页面。
# ======================================================================
CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
WEB_FILES = ("index.html", "style.css", "app.js", "chart.umd.min.js")
ACTIONS = ("pause", "resume", "clear", "soft_reset")


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """线程里冒出来的断连异常不当错误报（与 Handler.handle_one_request 同一目的）。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


class Handler(BaseHTTPRequestHandler):
    """极小的路由：/ 给页面，/api/* 给数据，其余按静态文件给。"""

    server_version = "QuietZoneStats/1.0"
    protocol_version = "HTTP/1.1"
    app = None                      # main() 里挂上来

    # ---- 输出助手 ----
    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _static(self, name):
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            self._send(404, "找不到 %s（前端文件应放在 %s）" % (name, WEB_DIR),
                       "text/plain; charset=utf-8")
            return
        with open(path, "rb") as fh:
            self._send(200, fh.read(),
                       CTYPES.get(os.path.splitext(name)[1].lower(),
                                  "application/octet-stream"))

    def log_message(self, fmt, *args):       # 关掉"每请求一行"的默认日志
        pass

    def handle_one_request(self):
        """把"客户端提前断开"当成正常结束，不打 traceback。

        页面每 1 s 轮询一次；关标签页 / 刷新 / 断网时会直接 RST 掉正在读的请求，
        标准库默认会把这类 ConnectionResetError 打成一大段 traceback。长时间运行时
        它会把控制台刷满，把真正的错误淹掉，所以这里就地吞掉。
        """
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                self._static("index.html")
            elif u.path == "/api/state":
                q = parse_qs(u.query)
                win = float(q.get("window", [str(getattr(self.app, "window_s", 900.0))])[0])
                self._json(self.app.state_payload(window_s=win))
            elif u.path == "/api/doa_samples":
                q = parse_qs(u.query)
                since = float(q.get("since", ["0"])[0])
                limit = int(q.get("limit", ["5000"])[0])
                self._json(self.app.doa_history_payload(since=since, limit=limit))
            elif u.path == "/api/calendar":
                q = parse_qs(u.query)
                self._json(self.app.calendar_payload(q.get("date", [None])[0]))
            elif u.path == "/api/export.csv":
                self._send(200, self.app.csv_text(), "text/csv; charset=utf-8", {
                    "Content-Disposition": 'attachment; filename="quietzone_stats_%s.csv"'
                                           % time.strftime("%Y%m%d_%H%M%S")})
            elif u.path.lstrip("/") in WEB_FILES:
                self._static(u.path.lstrip("/"))
            else:
                self._send(404, "404 %s" % u.path, "text/plain; charset=utf-8")
        except Exception as exc:             # 单次异常不打掉服务线程
            self._json({"ok": False,
                        "error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    do_HEAD = do_GET

    # ---- POST：控制 ----
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        action = q.get("cmd", [None])[0] or q.get("arg", [None])[0]
        if not action and u.path.startswith("/api/control/"):
            action = u.path.rsplit("/", 1)[-1]
        if action not in ACTIONS:
            self._json({"ok": False, "msg": "不支持的操作：%s（可选 %s）"
                        % (action, " / ".join(ACTIONS))}, 400)
            return
        try:
            ok, msg = self.app.control(action)
        except Exception as exc:
            ok, msg = False, "%s: %s" % (type(exc).__name__, exc)
        self._json({"ok": ok, "msg": msg, "action": action}, 200 if ok else 500)


def banner(app, host, port):
    src = app.src
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    lines = [
        "",
        "静域 QuietZone · 声源定位统计网页",
    ]
    if src.mode == "demo":
        lines.append("  ！！ 当前是【模拟演示】数据，不是真实测量；接板卡请用 --source serial")
    if src.mode == "serial":
        lines.append("  sound_loc 接口：UART %s，115200 8N1" % src.addr)
    lines += [
        "  数据源    : %s（%s）" % (src.label, src.mode),
        "  地址窗口  : %s" % src.addr,
        "  采样周期  : %.2f s（页面 1 s 刷一次）" % app.poll_s,
        "  打开页面  : http://%s:%d/" % (shown, port),
        "  状态接口  : http://%s:%d/api/state" % (shown, port),
        "  日历接口  : http://%s:%d/api/calendar?date=YYYY-MM-DD" % (shown, port),
        "  方位标定  : http://%s:%d/api/doa_samples" % (shown, port),
        "  导出 CSV  : http://%s:%d/api/export.csv" % (shown, port),
        "  停止      : Ctrl-C",
        "",
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="静域 QuietZone 统计网页：从 sound_loc UART 接口读取真实测量")
    ap.add_argument("--source", choices=("serial", "board", "demo"), default="serial",
                    help="serial = sound_loc UART（默认）；board = 旧 /dev/mem；demo = 模拟")
    ap.add_argument("--serial-port", default=os.environ.get("QZ_SERIAL_PORT"),
                    help="如 COM5；省略时自动寻找 CP210x")
    ap.add_argument("--baud", type=int, default=115200, help="UART 波特率，默认 115200")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--bind", default="127.0.0.1", help="监听地址；同局域网访问用 0.0.0.0")
    ap.add_argument("--poll", type=float, default=0.5, help="采样周期（秒）")
    ap.add_argument("--window", type=float, default=900.0, help="曲线默认时间窗（秒）")
    ap.add_argument("--log", default=None, help="每 10 s 一行 JSONL 的落盘路径（可选）")
    ap.add_argument("--history-dir", default=None,
                    help="按本地日期自动轮转为 qz-YYYY-MM-DD.jsonl 的历史目录（可选）")
    args = ap.parse_args(argv)

    if args.source == "board":
        try:
            src = BoardSource()
        except Exception as exc:            # 权限不足 / 不是 Zynq / 没上电
            sys.stderr.write("打不开旧版板卡寄存器窗口：%s\n" % exc)
            sys.stderr.write("  当前四麦定位请用 --source serial；只想看页面请用 --source demo\n")
            return 2
    elif args.source == "serial":
        try:
            src = SerialSource(port=args.serial_port, baud=args.baud)
        except Exception as exc:
            sys.stderr.write("打不开 sound_loc 串口：%s\n" % exc)
            sys.stderr.write("  查设备管理器中的 CP210x 端口，或显式传入 --serial-port COMx\n")
            return 2
    else:
        src = DemoSource()
    app = App(src, poll_s=args.poll)
    app.window_s = args.window
    app.log_path = args.log
    app.history_dir = args.history_dir
    Handler.app = app

    threading.Thread(target=app.run, name="qz-sampler", daemon=True).start()
    httpd = QuietThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.daemon_threads = True

    print(banner(app, args.bind, args.port))
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("收到 Ctrl-C，正在退出……")
    finally:
        app.stop_flag = True
        httpd.server_close()
        try:
            src.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
