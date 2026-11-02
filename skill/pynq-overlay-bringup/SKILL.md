---
name: pynq-overlay-bringup
description: 在 PYNQ（Zynq-7000 / ZynqMP）上加载并校验任意 Vivado 交付的 Overlay，把 XSA/HWH 里的 IP、地址与位域转成可执行的寄存器访问代码和冒烟测试；换题目、换板卡仍可复用。适用于带 PL 加速器的上板 bring-up，不适用于纯 PS 软件或只有裸机 ELF 的场景。
---

# PYNQ Overlay 加载与寄存器映射

## 1. 这个技能解决什么问题

Vivado 交付的是位流和硬件描述，PYNQ 交付的是 Overlay 对象。中间这段"IP 到底叫什么、基址是多少、位域几位、写进去有没有生效"通常靠猜。本技能把它固化成一条可判定的链：**加载 → 自检 → 按契约读写 → 写后回读 → 冒烟**，每一步都有明确输出。

## 2. 何时使用

- 板卡已有 PYNQ 镜像（2.7 或 3.x），需要在不写裸机 C 的前提下验证 PL 加速器。
- 手上只有 XSA/bit 和一张寄存器表，需要尽快得到能跑的 Python 调用代码。
- 需要在答辩或评审现场证明"PL 真的起来了、寄存器真的能写"。

## 3. 何时不使用

- 纯 PS 应用、没有 PL；或板卡只跑裸机 ELF（那种情况用 Vitis + JTAG）。
- 需要硬实时：PYNQ 跑在 Linux 用户态，GPIO 翻转抖动在毫秒级。
- 只有 .bit 没有 .hwh/.xsa：PYNQ 无法从位流反推 IP 名与地址，先回 Vivado 重新导出。

## 4. 前置条件

1. 镜像与板卡匹配；`pip show pynq` 与镜像版本一致（2.7 用 `.bit + .hwh`，3.x 可直接给 `.xsa`）。
2. 交付三件套：位流、硬件描述（`.hwh` 或 `.xsa`）、寄存器契约（基址/偏移/位域/读写属性）。
3. overlay 文件放在 `/home/xilinx` 下且可读；访问 `/dev/mem` 需要 root 或 sudo。
4. 先写下"第一个要证明的结论"，例如"版本寄存器读回 0x0001_0003"。

## 5. 工作流

### 5.1 先对齐三件套

位流与硬件描述必须来自同一次实现、同名同目录。否则 IP 名与地址对不上，而报错只会说"找不到 IP"，很难定位。

### 5.2 加载与自检

```python
from pynq import Overlay
ol = Overlay("/home/xilinx/overlays/design.xsa")   # PYNQ 2.7: Overlay("design.bit")
ol.download()
assert ol.is_loaded()
print(sorted(ol.ip_dict))     # 真实 IP 名，可能带层次前缀
print(ol.clock_dict)          # 时钟频率是否与约束一致
```

不要在代码里写死 `xxx_0`：Vivado 例化名、BD 层次名、PYNQ 归一化名三者可能不同，一律以 `ip_dict` 实测为准。

### 5.3 地址与位域

```python
from pynq import MMIO
mmio = MMIO(0x4000_0000, 0x1_0000)   # 基址与窗口长度取自 XSA 地址段
mmio.write(0x00, 0x1)
print(hex(mmio.read(0x00)))
```

带 `.hwh` 时优先用自动生成的 `register_map`，位域名来自 IP 包，不用手抄：

```python
rm = ol.reg_bank_0.register_map
rm.CTRL.AP_START = 1
```

`register_map` 的属性名以 IP 包为准；与文档不一致时改文档，不改代码。

### 5.4 写后回读

所有可写寄存器按"写 → 回读 → 比对"执行，不一致立刻抛异常并打印寄存器名、期望值、实际值。只写不读的流程会把总线问题拖到联调后期才暴露。

### 5.5 从接口契约生成调用骨架

把契约写成 CSV（`name,offset,width,access`），用一段模板生成带属性的访问类：

```python
FIELDS = [("CTRL", 0x00, 32, "rw"), ("STATUS", 0x04, 32, "ro")]
class Regs:
    def __init__(self, mmio):
        self.m = mmio
        self.off = {n: o for n, o, _, _ in FIELDS}
    def get(self, name):
        return self.m.read(self.off[name])
    def set(self, name, val):
        self.m.write(self.off[name], val)
        assert self.m.read(self.off[name]) == val, name
```

同一份 CSV 再生成 pytest 骨架：每个 rw 字段一条写读用例。契约变更时重新生成，测试跟着走。

### 5.6 冒烟与判定

冒烟脚本只做四件事：读版本 → 写一个安全寄存器并回读 → 触发一次最小事务 → 打印 PASS/FAIL 与耗时。退出码 0 才算通过。

## 6. 已知坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 加载报 "no such file" | `.bit` 与 `.hwh` 不同名或不同目录 | 同名同目录；3.x 直接给 `.xsa` |
| `ip_dict` 里找不到 `xxx_0` | 层次前缀或归一化名不同 | 打印 `ip_dict`，用真实名 |
| 写入无反应 | 时钟未使能或复位未释放 | 先读版本寄存器确认总线通，再解复位 |
| 读到全 0 / 全 F | 地址段未分配，或位流没下载 | 查 XSA 地址段与 `is_loaded()` |
| 进程直接被杀 | 读未映射地址触发 SLVERR→SIGBUS，Python 的 try/except 抓不住 | 存在性探测放到 fork 出的子进程里做 |
| `register_map` 属性报错 | 该 IP 未带寄存器描述 | 退回 `MMIO` + 契约表 |

## 7. 完成判定

- `is_loaded()` 为真，`ip_dict` 覆盖设计里的全部 IP。
- 每个 rw 寄存器写读一致，ro 寄存器默认值与规格一致。
- `clock_dict` 的频率与约束一致。
- 冒烟脚本在干净板子上一次跑通，退出码 0，耗时与预期同量级。

## 8. 验证记录

本工程（Vivado 2026.1 / xc7z020 / AX7020）使用 `0x4000_0000` 寄存器堆与 `0x4002_0000` 帧存两段地址；`src/host/hw_ifc.py` 的地址表与 BD 分配逐项一致，`--mock --selftest` 在本机通过。板卡实际走 JTAG + 裸机 ELF，未在 PYNQ 镜像上实机加载，故状态记为**部分验证**：地址与位域已核对，Overlay 加载未实测。

## 9. 迁移到其它工程

替换三处：overlay 路径、IP 名、契约 CSV。流程、回读规则、SIGBUS 探测方式与冒烟判定可直接复用。
