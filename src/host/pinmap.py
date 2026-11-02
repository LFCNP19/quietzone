import re, openpyxl, xml.etree.ElementTree as ET

pkg = r"D:\VIVA\2026.1\data\ip\xilinx\mig_7series_v4_2\data\fpga_tlib\zynq\7z\xc7z020clg400_pkg.xml"
xlsx = r"C:\Users\15718\Documents\xwechat_files\wxid_n41s8waic2a022_6dea\msg\file\2026-10\AX7010_AX7020管脚.xlsx"

# 1) package file: PAD -> (bank, iotype, iobloc)
root = ET.parse(pkg).getroot()
pad = {}
for bank in root.findall("BankInfo"):
    b = bank.get("Bank")
    for p in bank.findall("PadInfo"):
        pad[p.get("PADName")] = dict(bank=b, iotype=p.get("IOType"), iobloc=p.get("IOBLOC"), diff=p.get("DIFFPair"))

# 2) ALINX xlsx: net -> pad
wb = openpyxl.load_workbook(xlsx, data_only=True)
ws = wb["Sheet1"]
net2pad = {}
for r in range(2, ws.max_row+1):
    padname = ws.cell(r,1).value
    net = ws.cell(r,2).value
    if padname and net:
        net2pad[str(net).strip()] = str(padname).strip()

print("封装表引脚数:", len(pad), " ALINX 管脚表条目数:", len(net2pad))

# 3) J10 物理脚位: pin (2k+1) = EX_IO1_kN, pin (2k+2) = EX_IO1_kP
j10 = {}
for k in range(1, 18):
    j10[f"IO1_{k}N"] = 2*k+1
    j10[f"IO1_{k}P"] = 2*k+2

rows = []
for net, jpin in sorted(j10.items(), key=lambda kv: kv[1]):
    padname = net2pad.get(net, "?")
    info = pad.get(padname, {})
    rows.append((jpin, net, padname, info.get("bank","?"), info.get("iotype","?")))

print(f'{"J10脚":>5} {"网络名":<10} {"FPGA引脚":<6} {"Bank":<5} {"IO 功能名"}')
for jpin, net, padname, bank, iotype in rows:
    print(f"{jpin:>5} {net:<10} {padname:<6} {bank:<5} {iotype}")
