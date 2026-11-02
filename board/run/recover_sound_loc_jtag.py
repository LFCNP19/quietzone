#!/usr/bin/env python3
"""Recover a failed Zynq JTAG context and reload the QuietZone FPGA image."""
import os

import xsdb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BIT = os.environ.get("QZ_BIT", os.path.join(ROOT, "board", "bitstream", "sound_loc_integrated.bit"))
s = xsdb.start_debug_session()
try:
    print("CONNECT", s.connect(url="TCP:localhost:3121"), flush=True)
    print("TARGETS_BEFORE", s.targets(), flush=True)
    for target_id in (1, 2):
        try:
            s.targets(id=target_id)
            print(f"SYSTEM_RESET_{target_id}", s.rst("-s", type="system"), flush=True)
            print("TARGETS_AFTER_RESET", s.targets(), flush=True)
            break
        except Exception as exc:
            print(f"SYSTEM_RESET_{target_id}_SKIPPED", repr(exc), flush=True)
    try:
        s.targets("--set", filter="name =~ xc7z020")
        print("FPGA", s.fpga(file=BIT), flush=True)
    except Exception as exc:
        print("FPGA_SKIPPED", repr(exc), flush=True)
    print("TARGETS_FINAL", s.targets(), flush=True)
finally:
    xsdb.dispose()
