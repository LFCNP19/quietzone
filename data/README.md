# data/ —— 测试数据与参考结果

## golden/ —— 参考数据

`frame_window_256x8.coe`：256 点汉宁窗的 8 bit 量化值（0..255），
`w[n] = 0.5 - 0.5·cos(2πn/N)`，N=256。用途有两个：

1. 作为 PS 侧 GCC-PHAT 加窗的参考系数，核对 `main.c` 里的窗表；
2. 换成 `blk_mem_gen` IP 时可直接作为 COE 初始化文件。

不要手改这个文件。要改窗长或位宽，改 `src/host/gen_frame_window.py` 顶部的 `N` / `WB`
再重跑，脚本会把系数和 COE 一起重新生成。

## samples/ —— 现场样本

两个文件都是**原始遥测**，一行一个 JSON，未做平滑或抽样：

| 文件 | 行数 | 采集条件 | 能回答什么 |
| --- | ---: | --- | --- |
| `live_fsm_4s_human_hold2.jsonl` | 248 | 2026-10-22，人声持续说话触发分级，42.0 s | 状态怎么迁移、每档停留多久、各档声压范围 |
| `classifier_live_capture.jsonl` | 57 | 同一时段的分类器特征帧 | 语音/非语音特征（平坦度、谱质心、通量、低能量占比、峰值比、过零率）落在什么区间 |

字段说明（`live_fsm_*.jsonl`）：

| 字段 | 含义 |
| --- | --- |
| `ts` | Unix 秒（浮点），中位间隔 0.171 s |
| `state` / `state_name` | 0/1/2 → QUIET / WARN / ALARM |
| `spl_db` | 相对 A 计权声压（**未标定**，只能横向比较） |
| `speech_ms` / `other_ms` / `below_ms` | 说话、其他声音、低于门限的累计毫秒 |
| `classifier_conf` / `classifier_speech` | 分类器置信度与判决 |

复算方法见 `board/measurements/README.md`；结论只写到"相对比较"这一层，
绝对声压级要等标准声级计标定（见 `board/measurements/measured_vs_reference.md` 的待补齐项）。
