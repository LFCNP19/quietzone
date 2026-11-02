#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
静域 QuietZone —— PS 侧上报客户端（方案书 §6.1 数据模型 + §6.3 离线缓存补传）

它干什么
--------
1) 周期（默认 10 s）把寄存器堆里的统计量打包成 §6.1 的 report JSON，
   POST 到 <url>/api/v1/report；
2) 状态跳变时把事件打包成 §6.1 的 event JSON，POST 到 <url>/api/v1/event；
3) 发不出去（断网 / 接收端没起）就把这条 JSON 按时间顺序写进本地 SQLite 队列，
   之后每一轮上报前先把队列排空 —— 这就是"离线缓存补传"，队列在断电后不丢。

为什么这么实现
--------------
· 只用标准库（urllib + sqlite3），板子上不装任何第三方包；
· 队列保序：补传必须按时间顺序，先发不出去的那条不能跳过；
· 队列超上限（默认 20000 条）丢最老的，并把丢了多少打印出来，不静默丢数据；
· 隐私承诺的技术落点（§6.1）：以太网上只有这两个 JSON，原始音频从来没出过 PL。

用法
----
    # 板子上（配合 hw_ifc.py 的 board 后端，需要 root）
    sudo python3 reporter.py --url http://192.168.1.100:8090 --device-id SEAT-A-017

    # PC 上先看效果（不需要板卡）
    python reporter.py --mock --url http://127.0.0.1:8090 --duration 30

    # 离线缓存回环自测（不碰网络）
    python reporter.py --selftest

配合 python/cloud_server.py 一起用；两个都在本目录下直接跑。
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

import hw_ifc

DEFAULT_URL = "http://127.0.0.1:8090"
DEFAULT_DEVICE = "SEAT-A-017"
REPORT_PATH = "/api/v1/report"
EVENT_PATH = "/api/v1/event"

# §6.1 里 report 字段的必填项（selftest 按这张表核对）
REPORT_REQUIRED = [
    "device_id", "ts", "window", "occupied",
    "spl_avg", "spl_peak", "spl_avg_60s",
    "speech_ratio", "state",
    "disturb_cnt_today", "occupy_sec_today", "quiet_index",
]
# §6.1 里 event 字段的必填项
EVENT_REQUIRED = ["device_id", "ts", "from", "to", "dwell_prev", "trigger", "spl_at", "conf_at"]

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    kind   TEXT    NOT NULL,
    url    TEXT    NOT NULL,
    body   TEXT    NOT NULL,
    tries  INTEGER NOT NULL DEFAULT 0
);
"""


# ----------------------------------------------------------------------
# 离线缓存队列
# ----------------------------------------------------------------------
class Outbox(object):
    """发不出去的报文按时间顺序落盘的队列（SQLite 单文件，断电不丢）。"""

    def __init__(self, path, max_items=20000):
        self.path = path
        self.max = max_items
        self.dropped = 0
        self.db = sqlite3.connect(path)
        self.db.execute(QUEUE_SCHEMA)
        self.db.commit()

    def size(self):
        return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def put(self, url, kind, payload):
        body = json.dumps(payload, ensure_ascii=False)
        self.db.execute(
            "INSERT INTO outbox (ts, kind, url, body) VALUES (?,?,?,?)",
            (int(payload.get("ts", time.time())), kind, url, body))
        self.db.commit()
        while self.size() > self.max:
            self.db.execute("DELETE FROM outbox WHERE id = (SELECT MIN(id) FROM outbox)")
            self.db.commit()
            self.dropped += 1

    def peek(self):
        return self.db.execute(
            "SELECT id, url, body FROM outbox ORDER BY id LIMIT 1").fetchone()

    def ack(self, row_id):
        self.db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
        self.db.commit()

    def bump(self, row_id):
        self.db.execute("UPDATE outbox SET tries = tries + 1 WHERE id = ?", (row_id,))
        self.db.commit()

    def close(self):
        self.db.close()


def post_json(url, payload, timeout=3.0):
    """发一条 JSON。HTTP >= 400 或网络异常都抛出来，交给上层决定进不进队列。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    try:
        status = resp.status
        text = resp.read().decode("utf-8", "replace")
    finally:
        resp.close()
    if status >= 400:
        raise RuntimeError("HTTP %d %s" % (status, text[:120]))
    return status, text


def drain(outbox, sender, url=None, log=print, limit=64):
    """把队列里的补齐。遇到第一条发不出去就停（保序），返回补传成功条数。

    url 给定时按**当前**目标地址补传：队列里的旧账多半是因为地址写错或接收端没起才积下来的，
    操作员把 --url 改对这之后，旧账应该跟着新地址走，而不是继续往旧地址撞。
    不给 url 时退回用每条自己记录的那个地址。
    发不出去要说话：静默失败会让人以为队列坏了。
    """
    sent = 0
    while sent < limit:
        row = outbox.peek()
        if row is None:
            break
        row_id, stored_url, body = row
        try:
            sender(url or stored_url, json.loads(body))
        except Exception as exc:
            outbox.bump(row_id)
            log("  队首 1 条仍发不出去（%s）；队列保持 %d 条、顺序不变"
                % (exc, outbox.size()))
            break
        outbox.ack(row_id)
        sent += 1
    if sent:
        log("  补传成功 %d 条（队列剩 %d 条）" % (sent, outbox.size()))
    return sent


def deliver(outbox, sender, url, kind, payload, log=print):
    """先补传旧账，再发这一条；发不出去就入队。返回 (发出去了吗, 进了队列吗)。"""
    drain(outbox, sender, url, log=log)
    try:
        sender(url, payload)
        return True, False
    except Exception as exc:
        outbox.put(url, kind, payload)
        log("  上报失败，已缓存：%s（队列 %d 条）" % (exc, outbox.size()))
        return False, True


# ----------------------------------------------------------------------
# §6.1 报文构造
# ----------------------------------------------------------------------
def build_report(snap, device_id, window=10):
    """寄存器快照 -> §6.1 的 report JSON。"""
    return {
        "device_id": device_id,
        "ts": int(time.time()),
        "window": window,
        "occupied": snap["state"] != 0,
        "spl_avg": round(snap["spl_avg_10s_db10"] / 10.0, 1),
        "spl_peak": round(snap["spl_peak_10s_db10"] / 10.0, 1),
        "spl_avg_60s": round(snap["spl_avg_60s_db10"] / 10.0, 1),
        "speech_ratio": snap["speech_ratio"],
        "state": snap["state_name"],
        "disturb_cnt_today": snap["disturb_cnt"],
        "occupy_sec_today": snap["occupy_sec"],
        "quiet_index": snap["quiet_index"],
        # ---- v1.1 扩展字段：接收端允许忽略（§18.7：方位角随上报一起走）----
        "spl_now": round(snap["spl_now_db"], 1),
        "conf_now": snap["conf_now"],
        "mic_dom": snap["mic_dom_idx"] + 1,
        "mic_dom_flag": snap["mic_dom_flag"],
        "mic_rms": list(snap["mic_rms"]),
        "doa_angle": snap["doa_angle_deg"] if snap["doa_ok"] else None,
        "doa_delay_x": snap["doa_delay_x"],
        "doa_delay_y": snap["doa_delay_y"],
    }


def build_event(snap, prev_state, device_id, ev):
    """事件码 -> §6.1 的 event JSON。字段与 §4.7 的事件码一一对应。"""
    return {
        "device_id": device_id,
        "ts": int(time.time()),
        "from": hw_ifc.STATE_NAMES.get(prev_state, "?"),
        "to": ev["level_after_name"],
        "dwell_prev": snap["state_dwell_sec"],
        "trigger": ev["name"],
        "spl_at": round(snap["spl_now_db"], 1),
        "conf_at": snap["conf_now"],
    }


# ----------------------------------------------------------------------
# 主循环
# ----------------------------------------------------------------------
def make_sender(args):
    if args.dry_run:
        def fake(url, payload):
            print("  [dry-run] POST %s  %s"
                  % (url, json.dumps(payload, ensure_ascii=False)[:140]))
        return fake

    def real(url, payload):
        post_json(url, payload, timeout=args.timeout)
    return real


def make_regs(args):
    if args.mock:
        backend = hw_ifc.MockBackend()
    else:
        backend = hw_ifc.MmapBackend(args.base)
    return hw_ifc.RegBank(backend, verify=not args.no_verify)


def run(args):
    regs = make_regs(args)
    outbox = Outbox(args.queue, args.max_queue)
    sender = make_sender(args)
    base = args.url.rstrip("/")
    print("上报客户端：%s -> %s  设备 %s  窗口 %ds  队列 %s（当前 %d 条）"
          % ("mock" if args.mock else "board", base, args.device_id,
             args.window, args.queue, outbox.size()))

    t0 = time.time()
    n_rep = 0
    n_evt = 0
    while True:
        snap = regs.snapshot()
        prev_state = snap["state"]

        # 1) 事件：FIFO 里的事件码逐条上报（状态跳变本来就由 PL 侧产生事件码）
        for ev in regs.drain_events():
            payload = build_event(snap, prev_state, args.device_id, ev)
            deliver(outbox, sender, base + EVENT_PATH, "event", payload)
            prev_state = ev["level_after"]
            n_evt += 1
            print("  event #%d  %s -> %s  trigger=%s"
                  % (n_evt, payload["from"], payload["to"], payload["trigger"]))

        # 2) 周期报告
        payload = build_report(snap, args.device_id, args.window)
        deliver(outbox, sender, base + REPORT_PATH, "report", payload)
        n_rep += 1
        print("  report #%d  state=%-5s spl=%.1f dB(A) conf=%d 安静指数=%d 队列=%d"
              % (n_rep, payload["state"], payload["spl_avg"], payload["conf_now"],
                 payload["quiet_index"], outbox.size()))

        if args.once:
            break
        if args.duration and (time.time() - t0) >= args.duration:
            break
        time.sleep(args.window)

    if outbox.dropped:
        print("注意：队列超限丢掉了 %d 条最老的报文" % outbox.dropped)
    outbox.close()
    return 0


# ----------------------------------------------------------------------
# 自检：不碰网络，只验报文格式 + 离线缓存补传
# ----------------------------------------------------------------------
def selftest(args):
    fails = []

    def check(name, cond, extra=""):
        print("  [%s] %s%s" % ("ok" if cond else "FAIL", name,
                               ("  " + extra) if extra else ""))
        if not cond:
            fails.append(name)

    print("--- 1. §6.1 报文格式 ---")
    regs = hw_ifc.RegBank(hw_ifc.MockBackend(), verify=True)
    snap = regs.snapshot()
    rep = build_report(snap, DEFAULT_DEVICE, 10)
    miss = [k for k in REPORT_REQUIRED if k not in rep]
    check("report 必填字段齐全", not miss, "缺 " + str(miss) if miss else "")
    check("report.occupied 是布尔", isinstance(rep["occupied"], bool))
    check("report.state 是字符串", isinstance(rep["state"], str))
    ev_json = build_event(snap, 1, DEFAULT_DEVICE,
                          {"name": "speech_15s", "level_after_name": "WARN"})
    miss = [k for k in EVENT_REQUIRED if k not in ev_json]
    check("event 必填字段齐全", not miss, "缺 " + str(miss) if miss else "")
    check("event.from/to 是状态名", ev_json["from"] in hw_ifc.STATE_NAMES.values()
          and ev_json["to"] in hw_ifc.STATE_NAMES.values())

    print("--- 2. 离线缓存补传 ---")
    qp = os.path.join(os.path.dirname(os.path.abspath(args.queue)), "_reporter_selftest.db")
    if os.path.exists(qp):
        os.remove(qp)
    ob = Outbox(qp, max_items=100)
    quiet = (lambda *a, **k: None)

    def bad(_url, _payload):
        raise RuntimeError("network down")

    for _ in range(3):
        deliver(ob, bad, DEFAULT_URL + REPORT_PATH, "report", rep, log=quiet)
    check("断网时 3 条全部进队列", ob.size() == 3, "队列 %d" % ob.size())

    sent = []

    def good(_url, payload):
        sent.append(payload)

    n = drain(ob, good, log=quiet)
    check("网络恢复后 3 条全部补传", n == 3 and ob.size() == 0,
          "补传 %d 条，队列剩 %d" % (n, ob.size()))
    check("补传内容与入队的一致", all(p["device_id"] == DEFAULT_DEVICE for p in sent))

    # 队列上限：丢最老的，丢了多少要记下来
    ob2 = Outbox(qp, max_items=2)
    for _ in range(5):
        ob2.put(DEFAULT_URL + REPORT_PATH, "report", rep)
    check("队列超上限丢最老的并计数", ob2.size() == 2 and ob2.dropped == 3,
          "剩 %d 丢 %d" % (ob2.size(), ob2.dropped))
    ob2.close()
    ob.close()
    os.remove(qp)

    print()
    if fails:
        print("自检失败 %d 项：%s" % (len(fails), "、".join(fails)))
        return 1
    print("自检全过（报文格式 6 项 + 离线补传 4 项）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="静域 QuietZone 上报客户端（§6.1 数据模型 + §6.3 离线缓存补传）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help="接收端地址，默认 " + DEFAULT_URL)
    ap.add_argument("--device-id", default=DEFAULT_DEVICE, help="座位号，默认 " + DEFAULT_DEVICE)
    ap.add_argument("--mock", action="store_true", help="用内置模型代替 /dev/mem（PC 上验代码）")
    ap.add_argument("--base", type=lambda s: int(s, 0), default=hw_ifc.REG_BASE,
                    help="寄存器窗口物理基址，默认 0x40000000")
    ap.add_argument("--window", type=float, default=10.0, help="周期上报间隔（秒），默认 10")
    ap.add_argument("--timeout", type=float, default=3.0, help="单次 POST 超时（秒）")
    ap.add_argument("--queue", default="reporter_queue.db", help="离线队列文件")
    ap.add_argument("--max-queue", type=int, default=20000, help="队列上限（条）")
    ap.add_argument("--once", action="store_true", help="发一条 report 就退出（测试用）")
    ap.add_argument("--duration", type=float, default=0.0, help="跑多少秒后退出，0 = 一直跑")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不真发")
    ap.add_argument("--no-verify", action="store_true", help="关掉寄存器写回读校验")
    ap.add_argument("--selftest", action="store_true", help="离线缓存回环自测，不碰网络")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
