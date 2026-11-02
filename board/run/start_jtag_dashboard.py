#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Start the QuietZone JTAG bridge and the statistics web server together."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# 全部路径都相对提交包本身推导，换一台机器解压即可跑；需要覆盖时用环境变量。
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUN = HERE
PYTHON = sys.executable
VITIS = os.environ.get('QZ_VITIS', r'D:\VIVA\2026.1\Vitis\bin\vitis.bat')
BRIDGE = RUN / 'qz_jtag_bridge.py'
SERVER = ROOT / 'src' / 'host' / 'stats_server.py'
HISTORY_DIR = Path(os.environ.get('QZ_HISTORY_DIR', ROOT / 'data' / 'history'))
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
BRIDGE_PORT = 8765
WEB_PORT = 8088
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def main():
    if port_open(BRIDGE_PORT):
        raise SystemExit(f'port {BRIDGE_PORT} is already in use')
    if port_open(WEB_PORT):
        raise SystemExit(f'port {WEB_PORT} is already in use; stop the old stats_server first')

    bridge_log = open(RUN / 'qz_jtag_bridge.log', 'w', encoding='utf-8', errors='replace')
    web_log = open(RUN / 'qz_jtag_dashboard.log', 'w', encoding='utf-8', errors='replace')
    bridge = subprocess.Popen(
        [VITIS, '-s', str(BRIDGE)], cwd=str(RUN), stdout=bridge_log,
        stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
    web = None
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if bridge.poll() is not None:
                raise RuntimeError('JTAG bridge exited during startup')
            if port_open(BRIDGE_PORT):
                break
            time.sleep(0.2)
        else:
            raise TimeoutError('JTAG bridge did not open port 8765')
        web = subprocess.Popen(
            [PYTHON, str(SERVER), '--source', 'serial',
             '--serial-port', f'socket://127.0.0.1:{BRIDGE_PORT}',
             '--history-dir', str(HISTORY_DIR),
             '--port', str(WEB_PORT)],
            cwd=str(ROOT), stdout=web_log, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW)
        pid_file = RUN / 'qz_jtag_dashboard.pid'
        pid_file.write_text(f'bridge={bridge.pid}\nweb={web.pid}\n', encoding='ascii')
        print(f'BRIDGE_PID={bridge.pid} WEB_PID={web.pid}')
        print(f'WEB=http://127.0.0.1:{WEB_PORT}/')
        while True:
            if bridge.poll() is not None:
                raise RuntimeError(f'JTAG bridge exited with {bridge.returncode}')
            if web.poll() is not None:
                raise RuntimeError(f'web server exited with {web.returncode}')
            time.sleep(0.5)
    finally:
        for proc in (web, bridge):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        bridge_log.close()
        web_log.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
