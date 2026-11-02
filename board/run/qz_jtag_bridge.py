#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Expose the QuietZone on-board telemetry snapshot as a local QZ serial stream.

Run with: vitis -s qz_jtag_bridge.py
A TCP client such as stats_server.py can use socket://127.0.0.1:8765.
"""
import os
import re
import socket
import subprocess
import time

import xsdb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
ELF = os.environ.get("QZ_ELF", os.path.join(ROOT, "board", "elf", "sound_loc.elf"))
NM = os.environ.get(
    "QZ_ARM_NM",
    r"D:\VIVA\2026.1\Vitis\gnu\aarch32\nt\gcc-arm-none-eabi\bin\arm-none-eabi-nm.exe",
)
SYMBOL = "g_qz_mem_telemetry"
HOST = "127.0.0.1"
PORT = 8765
MAGIC = 0x515A544D


def symbol_address():
    out = subprocess.check_output([NM, "-S", "--defined-only", ELF], text=True)
    match = re.search(r"^([0-9A-Fa-f]+)\s+([0-9A-Fa-f]+)\s+\S\s+" + re.escape(SYMBOL) + r"\s*$", out, re.M)
    if not match:
        raise RuntimeError(f"symbol not found: {SYMBOL}")
    return int(match.group(1), 16), int(match.group(2), 16)


def values(session, address):
    words = session.mrd(address, "-v", word_size=4, size=32)
    if isinstance(words, int):
        words = [words]
    if len(words) != 32:
        raise RuntimeError(f"unexpected telemetry word count: {len(words)}")
    return words


def as_s32(value):
    return value - 0x100000000 if value & 0x80000000 else value


def snapshot(session, address):
    words = values(session, address)
    words2 = values(session, address)
    if words[0] != MAGIC or words2[0] != MAGIC:
        raise RuntimeError("telemetry magic missing; current ELF is not the JTAG telemetry build")
    if words[1] != 2 or words2[1] != 2:
        raise RuntimeError(f"unsupported telemetry version: {words[1]}/{words2[1]}")
    if words[2] != words2[2] or (words2[2] & 1):
        return None
    return words2


def qz_line(v):
    return ("QZ,kind={},seq={},az10={},spl10={},state={},trust={},"
            "t02_100={},t13_100={},thr10={},floor10={},bad={},wr={},rd={},"
            "r0_10={},r1_10={},r2_10={},r3_10={},"
            "clsconf={},clseff={},spklass={},flat1000={},centroid_hz={},"
            "flux1000={},low1000={},peak1000={},zcr10={},"
            "speech_ms={},other_ms={},below_ms={}\r\n").format(
        as_s32(v[3]), v[4], as_s32(v[5]), as_s32(v[6]), as_s32(v[7]),
        as_s32(v[8]), as_s32(v[9]), as_s32(v[10]), as_s32(v[11]),
        as_s32(v[12]), v[13], v[14], v[15], as_s32(v[16]), as_s32(v[17]),
        as_s32(v[18]), as_s32(v[19]), as_s32(v[20]), as_s32(v[21]),
        as_s32(v[22]), as_s32(v[23]), as_s32(v[24]), as_s32(v[25]),
        as_s32(v[26]), as_s32(v[27]), as_s32(v[28]), as_s32(v[29]),
        as_s32(v[30]), as_s32(v[31])).encode("ascii")


def main():
    addr, size = symbol_address()
    if size < 128:
        raise RuntimeError(f"telemetry symbol is too small: {size} bytes")
    print(f"TELEMETRY address=0x{addr:08X} size={size}", flush=True)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(1)
    print(f"BRIDGE listening on tcp://{HOST}:{PORT}", flush=True)

    session = xsdb.start_debug_session()
    try:
        session.connect(url="TCP:localhost:3121")
        session.targets("--set", filter="name =~ APU")
        while True:
            conn, peer = server.accept()
            print(f"CLIENT {peer[0]}:{peer[1]}", flush=True)
            try:
                with conn:
                    conn.settimeout(1.0)
                    last_counter = None
                    last_send = 0.0
                    while True:
                        try:
                            words = snapshot(session, addr)
                        except Exception as exc:
                            print(f"READ_ERROR {type(exc).__name__}: {exc}", flush=True)
                            time.sleep(0.5)
                            continue
                        if words is None:
                            time.sleep(0.02)
                            continue
                        now = time.monotonic()
                        if words[2] != last_counter or now - last_send >= 0.25:
                            conn.sendall(qz_line(words))
                            last_counter = words[2]
                            last_send = now
                        time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError) as exc:
                print(f"CLIENT_CLOSED {type(exc).__name__}: {exc}", flush=True)
    finally:
        server.close()
        xsdb.dispose()


if __name__ == "__main__":
    main()
