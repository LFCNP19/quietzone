---
name: pynq-dma-golden-verify
description: 在 PYNQ 上封装 AXI DMA / AXI-Stream 搬运与缓存一致性处理，定位通道不启动、长度不符、回读错位、超时等常见故障，并把黄金参考比对、吞吐与资源数据采集自动写成报告。适用于 PL 数据通路的上板验收，不适用于没有 DMA 的纯寄存器读写。
---

# PYNQ DMA 搬运、黄金比对与报告生成

## 1. 这个技能解决什么问题

"DMA 跑通了"经常只是"没报错"。本技能要求一次给出三样东西：**数据与黄金参考逐点一致、吞吐数字、可复现的报告行**。缓存一致性、长度单位、通道启动顺序这三个坑，占了 DMA 故障的绝大多数。

## 2. 何时使用

- PL 侧有 AXI DMA / AXI-Stream 数据通路，需要把大块数据搬到 PS。
- 需要回答"结果对不对、带宽多少、和参考差多少"。
- 需要把每次测量的数字自动写进报告，而不是手工抄。

## 3. 何时不使用

- 数据量小于一次 MMIO 读写的开销（几十字节），直接 `MMIO` 更快。
- 没有 DMA IP，或 DMA 未分配地址：先用 Overlay 技能把寄存器通路打通。
- 需要确定性延迟（微秒级抖动）：PYNQ 用户态调度保证不了，应改用裸机或 RT 内核。

## 4. 前置条件

1. DMA IP 已在 BD 中且地址已分配；方向（MM2S/S2MM）与数据位宽已知。
2. CMA 足够：`grep CmaTotal /proc/meminfo`，至少能同时放下输入和输出缓冲。
3. 黄金参考已准备好（`.npy` / `.coe` / 文本），并明确 dtype 与端序。
4. 时钟与复位已验证：先跑一次 MMIO 冒烟，确认 PL 起来。

## 5. 工作流

### 5.1 缓冲必须用 `allocate`

```python
from pynq import allocate
buf = allocate(shape=(n,), dtype=np.uint32)   # 物理连续
out = allocate(shape=(n,), dtype=np.uint32)
```

`np.zeros()` 拿到的页可能不连续，DMA 会静默搬错。CMA 不足时 `allocate` 直接抛异常，这是好事——比搬到一半失败强。

### 5.2 启动顺序：先 arm 接收，再发发送

```python
dma.recvchannel.transfer(out)
dma.sendchannel.transfer(buf)
dma.sendchannel.wait()
dma.recvchannel.wait()
```

S2MM 没先 arm 就发数据，轻则丢包重则挂死。`wait()` 一定带超时，避免整场演示卡住。

### 5.3 缓存一致性

- 发送前 `buf.flush()`：把 CPU 写的数据推到 DDR，DMA 才看得到。
- 接收后 `out.invalidate()`：丢掉 CPU 的旧缓存行，再读才是 DMA 写的数据。
- 用 `allocate()` 时驱动通常已代劳；一旦混用 `MMIO` 直接写缓冲、或复用同一块内存，必须自己补 flush/invalidate。

### 5.4 故障定位表

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| S2MM 不启动 | 接收通道未先 arm，或流缺 TLAST | 先 `recvchannel.transfer`；检查 PL 侧 TLAST |
| 传输长度不符 | 元素数当成字节数 | 统一用 `nbytes` 核对，打印两侧长度 |
| 回读是旧数据 | 缓存未失效 | `invalidate()` 后再读 |
| 偶发错位或乱码 | 缓冲非连续 | 只用 `allocate()` |
| `wait()` 超时 | 时钟/复位/中断未就绪 | 回 MMIO 冒烟，先确认 PL 活着 |
| 带宽远低于预期 | 单次搬运太小、突发被切断 | 单次 ≥ 4 KB，核对 AXI 位宽与突发长度 |

### 5.5 黄金比对

```python
ref = np.load("golden.npy")
got = np.frombuffer(out, dtype=np.uint32)[:ref.size]
bad = np.nonzero(got != ref)[0]
assert bad.size == 0, f"首个不一致 idx={bad[0]} got={got[bad[0]]:#x} exp={ref[bad[0]]:#x}"
```

比对规则要写清：定点数据整数全等，浮点用相对误差；比较前统一 dtype 与端序，否则"看起来不一样"往往只是解释方式不同。

### 5.6 采集并出报告

每次测量记录：负载字节数、耗时（`time.perf_counter`，多次取中位数）、吞吐 = 字节/时间、首个不一致位置。资源与时序从 `build/reports/*.rpt` 解析后并排写进同一张表，避免性能数字和资源数字来自两次不同的实现。

## 6. 已知坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 第一次对、第二次错 | 复用了未 invalidate 的缓冲 | 每次传输后 invalidate，或重新 allocate |
| 吞吐只有理论值 10% | 每次 256 B 的小包 + 频繁中断 | 合并成大包，关闭逐包中断 |
| 换板卡后全部超时 | 地址段或位宽变了 | 地址从 XSA 读，不要写死 |
| 报告数字与 `.rpt` 对不上 | 两次实现混用 | 报告里同时写位流 SHA-256 |

## 7. 完成判定

- 黄金比对零不一致（或有明确容差说明）。
- 吞吐、耗时、负载字节数三者自洽（字节/时间 = 吞吐）。
- 报告里的资源与时序能追到同一份 `.rpt` 和同一个位流。
- 脚本重跑两次：数据必须完全一致，吞吐允许小幅波动。

## 8. 验证记录

本工程的帧存通路（`0x4002_0000`，28800 字 RGB565）用 `hw_ifc.py --fb-pattern/--fb-check` 做过写读一致性核对；`data/golden/frame_window_256x8.coe` 与 `src/host/gen_frame_window.py` 的输出一致。吞吐测量与 DMA 部分按上述 API 编写，未在 PYNQ 镜像上实机运行，状态记为**部分验证**：比对方法与报告格式已验证，DMA 实机未验证。

## 9. 迁移到其它工程

替换：DMA IP 名、方向、数据位宽、黄金参考文件。启动顺序、flush/invalidate 规则、故障表与报告字段可直接复用。
