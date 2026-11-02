#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
静域 QuietZone —— 数据闭环接收端（方案书 §6.1 上报 + §6.2 管理视图）

干什么
------
· 收 python/reporter.py 发来的两种报文，落 SQLite：
    POST /api/v1/report   §6.1 周期上报
    POST /api/v1/event    §6.1 状态跳变事件
· 再把这些数据按 §6.2 的口径算出来给前端 / 脚本取：
    GET  /                          一个朴素的管理视图（设备列表 + 今日汇总 + 排行榜）
    GET  /api/v1/summary            每台设备最新一条 + 今日计数
    GET  /api/v1/seat/<id>/day      单座位日视图数据（小时序列 + 事件时间轴 + 占用时长）
    GET  /api/v1/leaderboard        今日最安静座位 Top10（正向激励，§6.2 排行榜）
    GET  /export.csv                导出当日明细（§6.2 管理视图的 CSV 导出）

为什么不用 Flask
----------------
§6.2 的技术选型写的是"Flask + SQLite"，但本工程 python/ 下所有程序都是**零第三方依赖**的
（stats_server.py 也一样）。这里用标准库 http.server + sqlite3 达到同样效果，
板子上不用装任何包。页面样式保持最简，正式的管理视图是 python/web/ 那个本地页面。

用法
----
    python cloud_server.py --port 8090 --db cloud.db
    # 浏览器打开 http://127.0.0.1:8090/
"""

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL, ts INTEGER NOT NULL, day TEXT NOT NULL,
        window REAL, occupied INTEGER,
        spl_avg REAL, spl_peak REAL, spl_avg_60s REAL,
        speech_ratio INTEGER, state TEXT,
        disturb_cnt_today INTEGER, occupy_sec_today INTEGER, quiet_index INTEGER,
        mic_dom INTEGER, doa_angle INTEGER, raw TEXT)""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL, ts INTEGER NOT NULL, day TEXT NOT NULL,
        frm TEXT, too TEXT, dwell_prev INTEGER, trigger TEXT,
        spl_at REAL, conf_at INTEGER, raw TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_rep_day ON reports (day, device_id)",
    "CREATE INDEX IF NOT EXISTS ix_evt_day ON events (day, device_id)",
]

REPORT_COLS = ["device_id", "ts", "day", "window", "occupied", "spl_avg", "spl_peak",
               "spl_avg_60s", "speech_ratio", "state", "disturb_cnt_today",
               "occupy_sec_today", "quiet_index", "mic_dom", "doa_angle", "raw"]
EVENT_COLS = ["device_id", "ts", "day", "frm", "too", "dwell_prev", "trigger",
              "spl_at", "conf_at", "raw"]


def day_of(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class Store(object):
    """SQLite 落盘 + 查询。多线程 HTTP 下每次操作各自开一个连接（sqlite3 不是线程安全的）。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with self._conn() as db:
            for stmt in SCHEMA:
                db.execute(stmt)

    def _conn(self):
        db = sqlite3.connect(self.path, timeout=10.0)
        db.row_factory = sqlite3.Row
        return db

    def add_report(self, p):
        ts = int(p.get("ts") or time.time())
        row = {
            "device_id": p.get("device_id", "?"),
            "ts": ts,
            "day": day_of(ts),
            "window": p.get("window"),
            "occupied": 1 if p.get("occupied") else 0,
            "spl_avg": p.get("spl_avg"),
            "spl_peak": p.get("spl_peak"),
            "spl_avg_60s": p.get("spl_avg_60s"),
            "speech_ratio": p.get("speech_ratio"),
            "state": p.get("state"),
            "disturb_cnt_today": p.get("disturb_cnt_today"),
            "occupy_sec_today": p.get("occupy_sec_today"),
            "quiet_index": p.get("quiet_index"),
            "mic_dom": p.get("mic_dom"),
            "doa_angle": p.get("doa_angle"),
            "raw": json.dumps(p, ensure_ascii=False),
        }
        with self.lock:
            with self._conn() as db:
                db.execute("INSERT INTO reports (%s) VALUES (%s)"
                           % (",".join(REPORT_COLS), ",".join("?" * len(REPORT_COLS))),
                           [row[c] for c in REPORT_COLS])
        return row

    def add_event(self, p):
        ts = int(p.get("ts") or time.time())
        row = {
            "device_id": p.get("device_id", "?"),
            "ts": ts,
            "day": day_of(ts),
            "frm": p.get("from"),
            "too": p.get("to"),
            "dwell_prev": p.get("dwell_prev"),
            "trigger": p.get("trigger"),
            "spl_at": p.get("spl_at"),
            "conf_at": p.get("conf_at"),
            "raw": json.dumps(p, ensure_ascii=False),
        }
        with self.lock:
            with self._conn() as db:
                db.execute("INSERT INTO events (%s) VALUES (%s)"
                           % (",".join(EVENT_COLS), ",".join("?" * len(EVENT_COLS))),
                           [row[c] for c in EVENT_COLS])
        return row

    def summary(self, day):
        out = []
        with self._conn() as db:
            devs = [r[0] for r in db.execute(
                "SELECT DISTINCT device_id FROM reports WHERE day=? ORDER BY device_id", (day,))]
            for d in devs:
                last = db.execute(
                    "SELECT * FROM reports WHERE day=? AND device_id=? ORDER BY ts DESC LIMIT 1",
                    (day, d)).fetchone()
                agg = db.execute(
                    "SELECT COUNT(*) n, AVG(quiet_index) qi, AVG(spl_avg) spl,"
                    " MAX(occupied) occ FROM reports WHERE day=? AND device_id=?",
                    (day, d)).fetchone()
                evn = db.execute(
                    "SELECT COUNT(*) n FROM events WHERE day=? AND device_id=?",
                    (day, d)).fetchone()["n"]
                out.append({
                    "device_id": d,
                    "samples": agg["n"],
                    "avg_quiet_index": round(agg["qi"], 1) if agg["qi"] is not None else None,
                    "avg_spl": round(agg["spl"], 2) if agg["spl"] is not None else None,
                    "occupied_today": bool(agg["occ"]),
                    "events": evn,
                    "latest": {
                        "ts": last["ts"], "state": last["state"],
                        "spl_avg": last["spl_avg"], "quiet_index": last["quiet_index"],
                        "speech_ratio": last["speech_ratio"],
                        "disturb_cnt_today": last["disturb_cnt_today"],
                        "occupy_sec_today": last["occupy_sec_today"],
                        "mic_dom": last["mic_dom"], "doa_angle": last["doa_angle"],
                    },
                })
        return out

    def seat_day(self, device_id, day):
        """单座位日视图数据：24 小时序列 + 事件时间轴 + 占用时长（§6.2 第一行那个页面）。"""
        with self._conn() as db:
            rows = db.execute(
                "SELECT ts, spl_avg, quiet_index, speech_ratio, occupied, window FROM reports"
                " WHERE day=? AND device_id=? ORDER BY ts", (day, device_id)).fetchall()
            evs = db.execute(
                "SELECT ts, frm, too, trigger, spl_at, dwell_prev FROM events"
                " WHERE day=? AND device_id=? ORDER BY ts", (day, device_id)).fetchall()
        hourly = []
        for h in range(24):
            hourly.append({"hour": h, "n": 0, "_qi": 0.0, "_spl": 0.0, "occupied_sec": 0})
        for r in rows:
            h = time.localtime(r["ts"]).tm_hour
            b = hourly[h]
            b["n"] += 1
            if r["quiet_index"] is not None:
                b["_qi"] += r["quiet_index"]
            if r["spl_avg"] is not None:
                b["_spl"] += r["spl_avg"]
            if r["occupied"]:
                # 占用时长按这条上报自己的 window 累加，不写死 10 s
                b["occupied_sec"] += int(r["window"] or 10)
        for b in hourly:
            # 注意先 pop 再判空：写成 "round(b.pop(...)/n) if n else None" 时，
            # n=0 的分支不会求值，_qi/_spl 这两个临时键就会漏在返回的 JSON 里。
            qi = b.pop("_qi")
            sp = b.pop("_spl")
            b["quiet_index"] = round(qi / b["n"], 1) if b["n"] else None
            b["spl_avg"] = round(sp / b["n"], 2) if b["n"] else None
        return {
            "device_id": device_id,
            "day": day,
            "samples": len(rows),
            "occupy_sec": sum(b["occupied_sec"] for b in hourly),
            "hourly": hourly,
            "events": [dict(e) for e in evs],
        }

    def leaderboard(self, day, top=10):
        with self._conn() as db:
            rows = db.execute(
                "SELECT device_id, AVG(quiet_index) qi, COUNT(*) n FROM reports"
                " WHERE day=? AND quiet_index IS NOT NULL"
                " GROUP BY device_id ORDER BY qi DESC LIMIT ?", (day, top)).fetchall()
        return [{"rank": i + 1, "device_id": r["device_id"],
                 "avg_quiet_index": round(r["qi"], 1), "samples": r["n"]}
                for i, r in enumerate(rows)]

    def export_rows(self, day, device_id=None):
        q = "SELECT * FROM reports WHERE day=?"
        a = [day]
        if device_id:
            q += " AND device_id=?"
            a.append(device_id)
        q += " ORDER BY device_id, ts"
        with self._conn() as db:
            return db.execute(q, a).fetchall()


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
def page_html(store, day):
    rows = store.summary(day)
    lb = store.leaderboard(day)
    parts = [
        "<!doctype html><meta charset=utf-8>",
        "<title>静域 QuietZone · 数据闭环</title>",
        "<style>body{font:14px/1.6 system-ui,'Microsoft YaHei';margin:32px;color:#1b2430;"
        "background:#f4f6f8}h1{font-size:20px}table{border-collapse:collapse;background:#fff;"
        "margin:12px 0 28px}th,td{border:1px solid #dde3ea;padding:6px 12px;text-align:left}"
        "th{background:#eef2f7}small{color:#5b6b7c}</style>",
        "<h1>静域 QuietZone · 数据闭环接收端</h1>",
        "<small>日期 " + day + "　设备 " + str(len(rows)) + " 台　"
        "（正式管理视图见工程里 python/web/ 那个本地页面）</small>",
        "<h2>今日汇总</h2>",
    ]
    if rows:
        parts.append("<table><tr><th>座位</th><th>采样数</th><th>平均安静指数</th>"
                     "<th>平均声压 dB(A)</th><th>当前状态</th><th>干扰次数</th>"
                     "<th>有人时长 s</th><th>最强麦</th><th>方位</th></tr>")
        for r in rows:
            lt = r["latest"]
            parts.append(
                "<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td>"
                "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (r["device_id"], r["samples"], r["avg_quiet_index"], r["avg_spl"],
                   lt["state"], lt["disturb_cnt_today"], lt["occupy_sec_today"],
                   lt["mic_dom"], lt["doa_angle"]))
        parts.append("</table>")
    else:
        parts.append("<p>今天还没有收到上报。先跑 python/reporter.py --mock --once。</p>")
    parts.append("<h2>今日最安静座位 Top10</h2>")
    if lb:
        parts.append("<table><tr><th>名次</th><th>座位</th><th>平均安静指数</th><th>采样数</th></tr>")
        for r in lb:
            parts.append("<tr><td>%d</td><td>%s</td><td>%s</td><td>%d</td></tr>"
                         % (r["rank"], r["device_id"], r["avg_quiet_index"], r["samples"]))
        parts.append("</table>")
    else:
        parts.append("<p>暂无数据。</p>")
    parts.append("<p><a href=\"/export.csv\">导出当日明细 CSV</a></p>")
    return "".join(parts)


class Handler(BaseHTTPRequestHandler):
    store = None
    server_version = "QuietZoneCloud/1.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("  %s %s"
                         % (self.address_string(), fmt % a))
        sys.stderr.write(os.linesep)

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=1),
                   "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            p = self._body()
        except Exception as exc:
            self._json(400, {"ok": False, "error": "JSON 解析失败: %s" % exc})
            return
        if path == "/api/v1/report":
            row = self.store.add_report(p)
            self._json(200, {"ok": True, "id": row["device_id"], "day": row["day"],
                             "ts": row["ts"]})
        elif path == "/api/v1/event":
            row = self.store.add_event(p)
            self._json(200, {"ok": True, "id": row["device_id"], "day": row["day"],
                             "ts": row["ts"], "transition": row["frm"] + "->" + str(row["too"])})
        else:
            self._json(404, {"ok": False, "error": "没有这个接口: " + path})

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        q = parse_qs(u.query)
        day = (q.get("date") or [day_of(time.time())])[0]
        if path in ("", "/"):
            self._send(200, page_html(self.store, day), "text/html; charset=utf-8")
        elif path == "/api/v1/summary":
            self._json(200, {"day": day, "devices": self.store.summary(day)})
        elif path.startswith("/api/v1/seat/"):
            dev = path[len("/api/v1/seat/"):]
            if dev.endswith("/day"):
                dev = dev[:-len("/day")]
                self._json(200, self.store.seat_day(dev, day))
            else:
                self._json(404, {"ok": False, "error": "用法：/api/v1/seat/<id>/day"})
        elif path == "/api/v1/leaderboard":
            self._json(200, {"day": day, "top": self.store.leaderboard(day)})
        elif path == "/export.csv":
            rows = self.store.export_rows(day, (q.get("device") or [None])[0])
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["device_id", "ts", "time", "spl_avg", "spl_peak", "spl_avg_60s",
                        "speech_ratio", "state", "disturb_cnt_today", "occupy_sec_today",
                        "quiet_index", "mic_dom", "doa_angle"])
            for r in rows:
                w.writerow([r["device_id"], r["ts"],
                            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                            r["spl_avg"], r["spl_peak"], r["spl_avg_60s"], r["speech_ratio"],
                            r["state"], r["disturb_cnt_today"], r["occupy_sec_today"],
                            r["quiet_index"], r["mic_dom"], r["doa_angle"]])
            fn = "quietzone_%s.csv" % day
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=" + fn)
            data = buf.getvalue().encode("utf-8-sig")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {"ok": False, "error": "没有这个路径: " + path})


def main(argv=None):
    ap = argparse.ArgumentParser(description="静域 QuietZone 上报接收端（§6.1 / §6.2）")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--bind", default="127.0.0.1", help="监听地址；同局域网访问用 0.0.0.0")
    ap.add_argument("--db", default="cloud.db", help="SQLite 落盘文件")
    args = ap.parse_args(argv)

    store = Store(args.db)
    Handler.store = store
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print("数据闭环接收端：http://%s:%d/   数据库 %s"
          % (args.bind, args.port, os.path.abspath(args.db)))
    print("  POST /api/v1/report   POST /api/v1/event")
    print("  GET  /api/v1/summary  /api/v1/seat/<id>/day  /api/v1/leaderboard  /export.csv")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
        print("停止。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
