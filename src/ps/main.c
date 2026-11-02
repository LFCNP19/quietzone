/******************************************************************************
 * main.c —— 图书馆声源定位装置 PS 侧主程序
 *----------------------------------------------------------------------
 * 硬件契约（与 PL 侧 acq_top.v 的约定，见 README 第七章）：
 *
 *   ① AXI GPIO @ 0x4120_0000（双通道，各 2 位宽）
 *        通道 1（输入）：PL 写的 wr_cnt —— "我装满几箱了"（模 4 循环计数）
 *        通道 2（输出）：PS 写的 rd_cnt —— "我读完几箱了"  （模 4 循环计数）
 *        差值 wr_cnt - rd_cnt（模 4）= 还没读的箱数，最多 2，满了 PL 会停写
 *
 *   ② AXI BRAM @ 0x4000_0000（16KB = 2 箱 × 8KB，乒乓）
 *        一箱 = 1024 个 64 位字；
 *        每个 64 位字 = 四路麦克风同一时刻的 16 位采样，按位拼装：
 *            [63:48] = ch3（mic_sd3）
 *            [47:32] = ch2（mic_sd2）
 *            [31:16] = ch1（mic_sd1）
 *            [15: 0] = ch0（mic_sd0）
 *        PS 的 AXI 口是 32 位宽，所以一个 64 位字 = 相邻两个 u32：
 *            p[2*i]   = 低 32 位 = {ch1, ch0}
 *            p[2*i+1] = 高 32 位 = {ch3, ch2}
 *
 *   ③ 采样率 fs = 50 MHz / 1024 = 48828.125 Hz（每 21ms 一箱）
 *
 *   ④ 响度门限（本节新增）
 *        每箱先算一次"响度"= 四路去直流后的 RMS 电平（dBFS，0 = 满量程，
 *        -60 = 很安静，-20 = 拍手级别）。只有当本箱电平同时满足
 *              level > 噪声本底 + GATE_MARGIN_DB   且   level > GATE_MIN_DB
 *        才认为"有拍手"，才继续跑 GCC-PHAT 并输出方位角；
 *        否则整箱直接丢弃，串口一个字都不打。
 *        噪声本底是自适应的：安静时快速下调、吵闹时缓慢上调，
 *        所以换个房间、开个空调不用重新调参。
 *
 * 算法：GCC-PHAT 广义互相关求两两时延差(TDOA)，再由两条对角线的时延
 *       解出方位角（见下面的几何推导）。
 *
 *----------------------------------------------------------------------
 * 麦克风摆放：正方形阵，边长 D = 5 cm（俯视图，四只麦克风在四个角上）
 *
 *        mic3 (西北) ---------------- mic2 (东北)      +Y 朝北
 *             |  \                      /  |           +X 朝东
 *             |    \   ← 对角线 1-3 →  /    |          θ 从 +X 逆时针量
 *             |      \                /      |
 *             |        \    中心     /        |
 *             |          \        /          |
 *             |            \    /            |
 *             |             ×  ← 两条对角线的交点
 *             |            /    \            |
 *             |          /        \          |
 *             |        /   ← 对角线 0-2 →    |
 *             |      /                \      |
 *             |    /                    \    |
 *        mic0 (西南) ---------------- mic1 (东南)
 *
 *   四个角点坐标（以阵列中心为原点，单位：米）
 *        mic0 = (-D/2, -D/2)     mic1 = (+D/2, -D/2)
 *        mic2 = (+D/2, +D/2)     mic3 = (-D/2, +D/2)
 *
 *   平面波从方位角 θ 入射（传播方向单位矢量 u = (cosθ, sinθ)），
 *   位于 p 的麦克风收到信号的时刻 t = -(p·u)/c（越靠近声源到得越早）。
 *   代入四个坐标：
 *        t02 = t0 - t2 = (D/c)·(cosθ + sinθ)      ← 对角线 0-2 的时延
 *        t13 = t1 - t3 = (D/c)·(sinθ - cosθ)      ← 对角线 1-3 的时延
 *   两式相加/相减：
 *        t02 + t13 = 2D/c · sinθ        t02 - t13 = 2D/c · cosθ
 *   于是
 *        θ = atan2( t02 + t13 , t02 - t13 )
 *
 *   注意：声速 c 和边长 D 在 atan2 的比值里全部约掉了，所以这个角度
 *   只取决于两条对角线时延的【比值】，跟 c 取 340 还是 343、跟你把边长
 *   量成 5.0 还是 5.2 cm 都无关（前提是四边形确实是正方形）。
 *
 *   ★ 唯一的硬约束：0 和 2 必须是同一条对角线的两端，1 和 3 是另一条。
 *     （也就是相邻通道不能配成一对，否则公式不成立。）
 *     如果接线顺序和上面不一样，只改 MIC_D_A0/A1/B0/B1 四个宏，别动公式。
 *----------------------------------------------------------------------
 ******************************************************************************/

#include <stdio.h>
#include <math.h>
#include "xparameters.h"
#include "xgpio.h"
#include "xil_types.h"
#include "xil_printf.h"
#include "xil_io.h"
#include "xil_cache.h"
#include "display_ctrl.h"
#include "state_logic.h"

/*============================================================================
 * 一、可调参数（按你的硬件实际情况改这里）
 *==========================================================================*/
/* 每箱采样点数 —— 以【实测的 PL 行为】为准，不是以 RTL 源码为准。
 *   板上位流实测：每帧只写 256 点（2 KB），而且第二块缓冲区偏移也不是 8 KB。
 *   详见 pl_probe_layout()。RTL 源码写的是 1024，两者不一致；
 *   只有位流重新生成后，才需要把这个数改回 1024。 */
#define FRAME_LEN      256           /* 每箱采样点数（必须与 PL 侧一致）        */
#define BRAM_BASE      XPAR_AXI_BRAM_CTRL_0_BASEADDR  /* 0x4000_0000 */
#define GPIO_BASE      XPAR_AXI_GPIO_0_BASEADDR       /* 0x4120_0000 */

#define FS_HZ          48828.125f    /* 50MHz / 1024，一箱 21ms                */
#define D_MIC          0.05f         /* 正方形【边长】（米）= 相邻两麦距离      */
                                     /*   对角线 = D_MIC × √2 ≈ 7.07 cm        */
#define C_SOUND        343.0f        /* 声速（米/秒），20°C 空气               */

/* 一条对角线上的最大时延（采样点数）= 对角线长 / c × fs
 *   = 0.0707 / 343 × 48828 ≈ 10.1 点
 * 互相关峰值若超出这个范围，说明这帧是噪声/干扰，丢弃不定位。 */
#define MAX_DELAY      12.0f

/* 对角线时延的标称值（采样点），≈10.07，用于一致性检查，不要改 */
#define TAU_DIAG       (D_MIC * 1.41421356f / C_SOUND * FS_HZ)
/* 一条边（相邻两麦）的标称时延（采样点）= D/c×fs ≈ 7.12，
 * 也是三麦模式下的孔径基准 A_nom。 */
#define TAU_SIDE       (D_MIC / C_SOUND * FS_HZ)

/* 麦克风健康判定：比最强一路低这么多 dB 就认为该路掉线 */
#define MIC_DEAD_MARGIN_DB  20.0f
#define MIC_DEAD_FLOOR_DB  (-78.0f)   /* 四路都这么安静时不下结论 */

/*----------------------------------------------------------------------------
 * PHAT 的工作频带（只在这个频带内做相位白化）
 *
 *   为什么要限带？PHAT 会把每个频点的幅度都拉成 1，等于"连噪声一起放大"。
 *   麦克风在 100 Hz 以下几乎没有有效信号，全是电源纹波/桌面震动/直流漂移，
 *   而 PHAT 却把这些频点当成"同样可信" —— 低频噪声因此被放大成互相关曲线
 *   上的大鼓包，峰位被拽偏几度很常见。掐掉低频是提升精度最划算的一步。
 *
 *   频点换算：bin = 频率 / (FS_HZ / N) = 频率 / 47.68 Hz
 *       6   → 286 Hz    掐掉隆隆声、气流声、桌面震动
 *       168 → 8.0 kHz   保留拍手的主要能量，同时避开高频空间混叠
 *   （对角线 7 cm 的空间无混叠上限只有 c/2D ≈ 2.4 kHz，但全带相位平均
 *     能把这个歧义压下去，所以高频不必掐得太狠。）
 *--------------------------------------------------------------------------*/
/* FRAME_LEN=256 时 bin 宽 = 48828.125/256 ≈ 190.7 Hz：
 *   bin 2  ≈ 381 Hz（掐掉隆隆声）
 *   bin 42 ≈ 8.0 kHz（拍手主要能量） */
#define PHAT_BIN_LO    2
#define PHAT_BIN_HI    42
/* PHAT 白化指数：1.0=全白化（低信噪比下会放大噪声），0.7 保留部分幅度权重 */
#define PHAT_BETA      0.7f
/* 配对有效性门限：互相关峰值 / 平均绝对值。直达声一般 >6，扩散噪声 1.5~3 */
#define PAIR_Q_MIN     2.2f

/* 方位角的系统偏差校正（度）。标定时把阵列正对东边拍手，
 * 若读到 +8 就一直偏 +8，把这里设成 -8.0f 即可。正数=整体往逆时针转。 */
#define AZ_OFFSET_DEG  0.0f

/* 两条对角线的四个端点：0↔2 一条，1↔3 另一条（与文件头几何图对应）
 *   ⚠ 相邻通道绝不能配成一对，否则公式失效、角度会乱跳。 */
#define MIC_D_A0       0             /* 西南角                                */
#define MIC_D_A1       2             /* 东北角                                */
#define MIC_D_B0       1             /* 东南角                                */
#define MIC_D_B1       3             /* 西北角                                */

/*----------------------------------------------------------------------------
 * 响度门限（只在"够响"时才输出方位角）
 *
 *   电平 = 20·log10(四路去直流 RMS / 32768)，单位 dBFS：
 *       0 dB  = 满量程（削顶）
 *     -20 dB  ≈ 拍手、近距离说话
 *     -45 dB  ≈ 安静办公室
 *     -65 dB  ≈ 深夜卧室
 *   判定条件（两条都要满足）：
 *       level > 噪声本底 + GATE_MARGIN_DB      比"平时"明显响
 *       level > GATE_MIN_DB                    绝对音量也不小（防死寂房间误触发）
 *
 * 调参建议：上板后把 VERBOSE_QUIET 置 1，看安静时的 lvl 值，
 *   把 GATE_MIN_DB 设成比它高 6~10 dB；拍手时再看 lvl，应该比它高 20 dB 以上。
 *   如果拍手有时不触发 → 调小 GATE_MARGIN_DB（8）；如果安静时误触发 → 调大（16）。
 *--------------------------------------------------------------------------*/
#define GATE_MARGIN_DB    4.0f       /* 高出噪声本底多少 dB 才算"响"           */
                                     /*   12 太苛刻：轻声拍手常常只高 8~10 dB，
                                     *   结果"拍了不理"。6 dB 已足够区分
                                     *   "日常底噪"和"有人拍了一下"。          */
#define GATE_MIN_DB     -60.0f       /* 绝对下限 dBFS，低于它一律不理           */
                                     /*   比安静房间底噪低 10 dB 左右，
                                     *   死寂房间里的一声轻响也能触发。        */
#define FLOOR_INIT_DB   -70.0f       /* 噪声本底初值（上电标定后会被覆盖）      */
#define FLOOR_RISE       0.02f       /* 本底向上爬的比例（每箱）                */
#define FLOOR_FALL       0.30f       /* 本底向下掉的比例（每箱）                */
#define FLOOR_MAX_UP     0.10f       /* 本底每箱最多上调多少 dB（≈4.8 dB/秒）   */
                                     /*   有它，持续吵闹也不会把本底抬飞，      */
                                     /*   拍手灵敏度不会自己变迟钝              */

/* 上电标定帧数：开机先吃 16 帧（≈0.35 秒）摸清环境本底，这期间不输出任何东西。
 * ★ 这一步是必须的：如果拿"第一帧"当本底，而第一帧恰好是 PL 刚启动、I2S 时钟
 *   还没稳的全零数据，本底就会被记成 -90 dBFS，门限掉到 -40，于是满屋子的
 *   日常噪声全都算"拍手" —— 表现就是"没人出声却一直打印角度"。
 *   取 16 帧的中位数，个别帧是静音或正好有人拍手都不影响结果。 */
#define CAL_FRAMES      16

/* 安静时是否也打一行电平（仅用于标定门限；正式用请置 0，保证"安静时零输出"） */
#define VERBOSE_QUIET    0

/* 置 1：在每个方位后面再打一行 t02/t13/一致性检查，用于排查算法问题 */
#define DEBUG_DLY        0

/* 同一次"响"最多连打多少帧（25 帧 ≈ 0.5 秒）。
 * 拍手只响 1~5 帧，远远够用；而空调启动、有人一直说话这类"持续大声"，
 * 打到 25 帧就闭嘴，必须等它安静下来才重新开始 —— 免得刷屏。 */
#define BURST_MAX_FRAMES 25

/* 置 1：每 20 帧打一次四路原始采样，用来验证数据链路。
 * ★ 正常跑请置 0，否则安静时也会冒出 raw 行，容易被误认为"没声音也在输出" */
#define DEBUG_RAW        0

/*----------------------------------------------------------------------------
 * 置 1 = 上板体检模式（现在就是 1，先把硬件问题查清楚再改回 0）
 *
 *   这个模式下【完全不看门限、不算方位】，只做一件事：
 *   每 10 帧把四路采样各自的电平、过零率、峰值原样打印出来。
 *   目的是回答一个非黑即白的问题 —— 到底是"麦克风没送对数据"，
 *   还是"数据是好的、门限调错了"。算法本身不会凭空造出 -10 dB。
 *
 *   判读方法见 README 第八章；查完把这里改回 0 就是正常定位程序。
 *--------------------------------------------------------------------------*/
#define DIAG_MODE        0

/*----------------------------------------------------------------------------
 * 置 1 = 只看响度（现在就是这个）
 *   不算方位、不过门限、不打任何诊断，每 5 帧（≈0.1 秒）打一行电平条：
 *
 *       lvl  -13 dB  [################################--------]
 *
 *   条子长度按 -80 dB（空）~ 0 dB（满）线性映射到 40 格。
 *   安静房间应当只有两三格（-70 ~ -60）；对着麦克风说话大约十几格。
 *   如果没人出声却一直几乎顶满 —— 那就是数据本身不对，不是响度算错。
 *--------------------------------------------------------------------------*/
#define LEVEL_ONLY       0

/* 版本标记：上板后先看启动横幅里的这一行，确认板子里跑的是不是最新 ELF */
#define BUILD_TAG      "QZ-INTEGRATED 2026-11-01 warn4s+release2s3s+classifier-v2+telemetry-v2+mic-health+3mic-doa+plprobe"

/* 圆屏上的数字与色环最多每秒提交一次。声音采样、状态判定和网页遥测
 * 仍按原始 1024 点帧运行。灯环每帧计算、隔帧真正刷新，约 24 Hz。 */
#define DISPLAY_REFRESH_FRAMES 48
#define LED_BREATH_WARN_PHASE_STEP  344u   /* 0.25 Hz @ 47.68 帧/s */
#define LED_BREATH_ALARM_PHASE_STEP 481u   /* 0.35 Hz @ 47.68 帧/s */
static u32 display_hold_frames = 0;
static u16 led_phase = 0;
static u32 led_update_toggle = 0;

/*============================================================================
 * 二、全局工作区（static 放 .bss，不占栈；Cortex-A9 栈默认够用）
 *==========================================================================*/
static XGpio Gpio;                            /* AXI GPIO 驱动句柄            */
static u32   rd_cnt = 0;                      /* 我（PS）读完了几箱，模 4     */

static s16 ch[4][FRAME_LEN];                  /* 四路原始采样（16 位有符号）  */

/* 机器可读遥测附加量。只记录 frame_level_dbfs() 已经算出的数值，不参与门控。 */
static float ch_level_dbfs[4] = {-90.0f, -90.0f, -90.0f, -90.0f};
/* 每帧更新的麦克风健康掩码：bit i = CHi 这一帧有有效信号。
 * 只用 frame_level_dbfs() 已经算好的四路电平判定，不额外做 FFT。 */
static unsigned g_mic_mask   = 0xFu;
static float    g_last_resid = 0.0f;   /* 三麦最小二乘归一化残差 */
static float    g_last_ap    = 0.0f;   /* 实测孔径 / TAU_SIDE    */
/* 现场诊断用：最近一帧六对时延与峰锐度（单位：采样点）。
 * g_qz_dbg 放在固定镜像里，可用 xsdb 直接 mrd 出来看，不占遥测协议。 */
static float     g_dbg_lag[6];
static float     g_dbg_qual[6];
volatile u32     g_qz_dbg[16];
static int   g_display_state = ST_IDLE;
static int   g_display_spl10 = 0;
static int   g_speech_conf = 0;
static int   g_speech_eff = 0;
static int   g_speech_state = 0;
static int   g_feat_flat1000 = 0;
static int   g_feat_centroid_hz = 0;
static int   g_feat_flux1000 = 0;
static int   g_feat_low1000 = 0;
static int   g_feat_peak1000 = 0;
static int   g_feat_zcr10 = 0;
static unsigned g_speech_ms = 0;
static unsigned g_other_ms = 0;
static unsigned g_below_ms = 0;

/* JTAG 内存遥测区：固定布局供主机通过现有 USB SLAVE/JTAG 读取。
 * 只镜像已有遥测值，不参与门控、定位或显示计算。 */
#define QZ_MEM_MAGIC   0x515A544DU
#define QZ_MEM_VERSION 2U
typedef struct {
    volatile u32 magic;
    volatile u32 version;
    volatile u32 counter;
    volatile s32 kind;
    volatile s32 seq;
    volatile s32 az10;
    volatile s32 spl10;
    volatile s32 state;
    volatile s32 trust;
    volatile s32 t02_100;
    volatile s32 t13_100;
    volatile s32 thr10;
    volatile s32 floor10;
    volatile s32 bad;
    volatile s32 wr;
    volatile s32 rd;
    volatile s32 r0_10;
    volatile s32 r1_10;
    volatile s32 r2_10;
    volatile s32 r3_10;
    volatile s32 speech_conf;
    volatile s32 speech_eff;
    volatile s32 speech_state;
    volatile s32 flat1000;
    volatile s32 centroid_hz;
    volatile s32 flux1000;
    volatile s32 low1000;
    volatile s32 peak1000;
    volatile s32 zcr10;
    volatile s32 speech_ms;
    volatile s32 other_ms;
    volatile s32 below_ms;
} qz_mem_telemetry_t;

qz_mem_telemetry_t __attribute__((used, aligned(16))) g_qz_mem_telemetry = {
    .magic = QZ_MEM_MAGIC,
    .version = QZ_MEM_VERSION,
};

/* FFT 复数工作区（float 精度对本项目足够） */
#define N  FRAME_LEN
static float re0[N], im0[N];                  /* 麦克风 A 的频谱              */
static float re1[N], im1[N];                  /* 麦克风 B 的频谱              */
static float tw_re[N/2], tw_im[N/2];          /* 旋转因子表，只算一次          */
static float hann_tab[N];                    /* 汉宁窗表，只算一次            */

/*============================================================================
 * 三、FFT：迭代基-2，输入输出都是就地(in-place)复数
 *     正变换：频谱；逆变换用共轭技巧 ifft(x)=conj(fft(conj(x)))/N
 *==========================================================================*/
static void fft_init(void)
{
    /* 预生成旋转因子 tw[k] = e^(-j·2πk/N)，供所有级共用 */
    for (int k = 0; k < N/2; k++) {
        float ang = -2.0f * 3.14159265f * (float)k / (float)N;
        tw_re[k] = cosf(ang);
        tw_im[k] = sinf(ang);
    }
    /* 预生成汉宁窗 w[n] = 0.5·(1 - cos(2πn/N))（周期窗），
     * 作用：压低帧两端的突变（频谱泄漏），GCC-PHAT 前加窗是标配 */
    for (int n = 0; n < N; n++)
        hann_tab[n] = 0.5f * (1.0f - cosf(2.0f * 3.14159265f * (float)n / (float)N));
}

/* 位反转置换：FFT 第一步，把下标按二进制倒序重排 */
static void bit_reverse(float *re, float *im)
{
    for (u32 i = 1, j = 0; i < N; i++) {
        u32 bit = N >> 1;
        for (; j & bit; bit >>= 1)  j ^= bit;
        j |= bit;
        if (i < j) {                /* 交换 re[i]<->re[j], im[i]<->im[j]     */
            float t;
            t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
}

static void fft(float *re, float *im)
{
    bit_reverse(re, im);
    /* len = 当前蝶形的跨度：2,4,8,...,1024 */
    for (u32 len = 2; len <= N; len <<= 1) {
        u32 half = len >> 1;
        u32 step = N / len;               /* 旋转因子表步进                   */
        for (u32 i = 0; i < N; i += len) {
            for (u32 k = 0; k < half; k++) {
                u32 a = i + k;            /* 蝶形上半                         */
                u32 b = a + half;         /* 蝶形下半                         */
                float wr = tw_re[k * step];
                float wi = tw_im[k * step];
                /* 复数乘法：t = w * X[b] */
                float tr = wr * re[b] - wi * im[b];
                float ti = wr * im[b] + wi * re[b];
                re[b] = re[a] - tr;       /* X[b] = X[a] - t                  */
                im[b] = im[a] - ti;
                re[a] += tr;              /* X[a] = X[a] + t                  */
                im[a] += ti;
            }
        }
    }
}

/*============================================================================
 * 四、GCC-PHAT：广义互相关-相位变换法，返回两路信号的时延（单位：采样点）
 *     返回值 = t_chA - t_chB（A 比 B 晚到为正）
 *==========================================================================*/
static float gcc_phat_q(const s16 *xa, const s16 *xb, float *qual)
{
    /* ① 去 DC + 加窗：把 s16 转成 float 装进 FFT 缓冲 */
    float mean_a = 0.0f, mean_b = 0.0f;
    for (int i = 0; i < N; i++) { mean_a += xa[i]; mean_b += xb[i]; }
    mean_a /= N;  mean_b /= N;
    for (int i = 0; i < N; i++) {
        re0[i] = ((float)xa[i] - mean_a) * hann_tab[i];
        re1[i] = ((float)xb[i] - mean_b) * hann_tab[i];
        im0[i] = 0.0f;  im1[i] = 0.0f;
    }

    fft(re0, im0);
    fft(re1, im1);

    /* ③ 互功率谱 + PHAT-beta 白化：
     *    R[k] = X·conj(Y) / |X·conj(Y)|^beta
     *    全白化（beta=1）在低信噪比、混响重的房间里会把每个频点的噪声
     *    等权放大，互相关峰变成随机跳点 —— 拍手这类"响但不够相干"的
     *    声音尤其明显：六对时延互相打架，最小二乘只能给出很小的孔径和
     *    很大的残差。取 beta=0.7 保留一部分幅度权重，真实直达声的峰
     *    才压得住噪声。 */
    for (int k = 0; k < N; k++) {
        float cr = re0[k]*re1[k] + im0[k]*im1[k];
        float ci = im0[k]*re1[k] - re0[k]*im1[k];
        float mag = sqrtf(cr*cr + ci*ci) + 1e-9f;
        float g   = 1.0f / powf(mag, PHAT_BETA);
        re0[k] = cr * g;
        im0[k] = ci * g;
    }

    /* ③' 限带 */
    for (int k = 0; k <= N/2; k++) {
        if (k < PHAT_BIN_LO || k > PHAT_BIN_HI) {
            re0[k] = 0.0f;  im0[k] = 0.0f;
            if (k) { re0[N-k] = 0.0f;  im0[N-k] = 0.0f; }
        }
    }

    /* ④ 逆变换 */
    for (int k = 0; k < N; k++) im0[k] = -im0[k];
    fft(re0, im0);
    for (int k = 0; k < N; k++) { re0[k] /= N; im0[k] = -im0[k] / N; }

    /* ⑤ 找峰：只在物理可能的时延窗 [-TAU_DIAG-1, +TAU_DIAG+1] 内找 */
    const int SRCH = (int)(TAU_DIAG + 1.0f);
    int   best_i = 0;
    float best_v = -1e30f;
    for (int i = 0; i <= SRCH; i++)
        if (re0[i] > best_v) { best_v = re0[i]; best_i = i; }
    for (int i = N - SRCH; i < N; i++)
        if (re0[i] > best_v) { best_v = re0[i]; best_i = i; }

    /* ⑤' 峰锐度：峰值 / 整条互相关的平均绝对值。
     *    干净直达声能到 6 以上；扩散噪声一般 1.5~3。用它当配对门限，
     *    比"孔径必须等于标称值"可靠得多 —— 后者会把正对中垂线的
     *    合法声源（孔径本来就接近 0）一律判成可疑。 */
    if (qual) {
        float acc = 0.0f;
        for (int k = 0; k < N; k++) acc += fabsf(re0[k]);
        acc /= (float)N;
        *qual = (acc > 1e-12f) ? best_v / acc : 0.0f;
    }

    /* ⑥ 抛物线插值 */
    int   ip = (best_i + N - 1) % N;
    int   in = (best_i + 1) % N;
    float ym = re0[ip], y0 = re0[best_i], yp = re0[in];
    float den = ym - 2.0f*y0 + yp;
    float delta = (fabsf(den) > 1e-12f) ? 0.5f*(ym - yp)/den : 0.0f;
    if (delta >  1.0f) delta =  1.0f;
    if (delta < -1.0f) delta = -1.0f;

    /* ⑦ 换算成带符号时延 */
    float tau = (best_i <= N/2) ? (float)best_i + delta
                                : (float)best_i - (float)N + delta;
    return tau;
}

/*============================================================================
 * 四'、阵列几何 + 最小二乘 TDOA 求解（坏通道降级用）
 *
 *  约定（与文件头几何图一致，坐标以边长 D 归一化）
 *      CH0 西南(-0.5,-0.5)   CH1 东南(+0.5,-0.5)
 *      CH2 东北(+0.5,+0.5)   CH3 西北(-0.5,+0.5)
 *
 *  任意一对 (a,b)：t_ab = gcc_phat(ch_a, ch_b)（单位：采样点）
 *      t_ab = A·(kx·cosθ + ky·sinθ)
 *      kx = -(x_a - x_b)   ky = -(y_a - y_b)   A = D·fs/c
 *  把可用配对的方程堆起来做最小二乘，解出
 *      X = A·cosθ   Y = A·sinθ   →   θ = atan2(Y, X)
 *
 *  为什么这样写：
 *    · 四麦全好时只取两条对角线（0↔2、1↔3），解得
 *        X = (t02-t13)/2   Y = (t02+t13)/2
 *      θ = atan2(t02+t13, t02-t13)，与原来的公式逐位等价，老行为不变。
 *    · 坏一路时（例如 CH1 的 SD 线上根本没有信号），三条边仍给出
 *      3 个方程解 2 个未知量，多出来的那个方程就是真正的自检残差。
 *    · θ 只由 X、Y 的比值决定，与 A 的绝对大小无关 ——
 *      所以阵列间距的实际值和标称值不一致时，角度也不会被带偏，
 *      这正是此前"每帧都被判可疑、页面永远低置信"的根源。
 *==========================================================================*/
typedef struct {
    float       az_deg;        /* 解出的方位角（未加 AZ_OFFSET_DEG）      */
    float       t_pair[2];     /* 前两对的实测时延（采样点）              */
    signed char pair_a[2];     /* 这两对分别是哪两路                      */
    signed char pair_b[2];
    float       aperture;      /* A 的估计值（采样点）                    */
    float       resid;         /* 归一化残差：0 = 三条边完全自洽          */
    float       qmin;          /* 参与解算的配对里最弱的峰锐度            */
    int         npair;         /* 实际参与解算的配对数                    */
    unsigned    mask;          /* 本帧有效的麦克风掩码                    */
} doa_result_t;

static int doa_count(unsigned m)
{
    int n = 0;
    for (int i = 0; i < 4; i++) if ((m >> i) & 1u) n++;
    return n;
}

static int doa_solve(unsigned mask, doa_result_t *r)
{
    /* 配对优先级：两条对角线在前，四条边在后 */
    static const signed char PA[6] = {0, 1, 0, 1, 0, 2};
    static const signed char PB[6] = {2, 3, 1, 2, 3, 3};
    static const float MX[4] = {-0.5f, +0.5f, +0.5f, -0.5f};
    static const float MY[4] = {-0.5f, -0.5f, +0.5f, +0.5f};
    float t[6], kx[6], ky[6], wt[6], qq[6];
    int   ok[6];
    int   np = 0;

    r->mask      = mask;
    r->az_deg    = 0.0f;
    r->aperture  = 0.0f;
    r->resid     = 0.0f;
    r->npair     = 0;
    r->qmin      = 0.0f;
    r->t_pair[0] = 0.0f;  r->t_pair[1] = 0.0f;
    r->pair_a[0] = 0;     r->pair_b[0] = 2;
    r->pair_a[1] = 1;     r->pair_b[1] = 3;

    /* 先把六对都量一遍（坏通道的对直接跳过），再按峰锐度筛 */
    for (int i = 0; i < 6; i++) {
        const int a = PA[i], b = PB[i];
        ok[i] = 0;
        g_dbg_lag[i]  = 0.0f;
        g_dbg_qual[i] = 0.0f;
        if (!(((mask >> a) & 1u) && ((mask >> b) & 1u))) continue;

        float q = 0.0f;
        const float tau = gcc_phat_q(ch[a], ch[b], &q);
        g_dbg_lag[i]  = tau;
        g_dbg_qual[i] = q;

        /* 门限一：时延不能超过物理上限（对角线）
         * 门限二：峰必须比整条互相关的平均值明显突出 */
        if (fabsf(tau) > TAU_DIAG + 0.5f) continue;
        if (q < PAIR_Q_MIN) continue;

        t[np]  = tau;
        qq[np] = q;
        kx[np] = -(MX[a] - MX[b]);
        ky[np] = -(MY[a] - MY[b]);
        /* 权重：基线越长越可信；峰越锐越可信（上限 6，防止一对独大） */
        const float base = sqrtf(kx[np]*kx[np] + ky[np]*ky[np]);
        const float qf   = (q > 6.0f) ? 1.0f : (q / 6.0f);
        wt[np] = base * (0.25f + 0.75f * qf);
        ok[i] = 1;
        np++;
    }

    /* 上报用：优先给 0-2 / 1-3 两条对角线；被门限刷掉时退回其它有效对 */
    int have0 = 0, have1 = 0;
    for (int i = 0; i < 6; i++) {
        if (!ok[i]) continue;
        if (i == 0) { r->t_pair[0] = g_dbg_lag[i]; r->pair_a[0] = PA[i]; r->pair_b[0] = PB[i]; have0 = 1; }
        if (i == 1) { r->t_pair[1] = g_dbg_lag[i]; r->pair_a[1] = PA[i]; r->pair_b[1] = PB[i]; have1 = 1; }
    }
    if (!have0 || !have1) {
        for (int i = 2; i < 6; i++) {
            if (!ok[i]) continue;
            if (!have0) { r->t_pair[0] = g_dbg_lag[i]; r->pair_a[0] = PA[i]; r->pair_b[0] = PB[i]; have0 = 1; }
            else if (!have1) { r->t_pair[1] = g_dbg_lag[i]; r->pair_a[1] = PA[i]; r->pair_b[1] = PB[i]; have1 = 1; }
        }
    }

    if (np < 2) return 0;              /* 可用配对不够：不给角度 */

    /* 加权法方程 [Σw·kx² Σw·kxky; Σw·kxky Σw·ky²]·[X;Y] = [Σw·kx·t; Σw·ky·t] */
    float sxx = 0.0f, sxy = 0.0f, syy = 0.0f, bx = 0.0f, by = 0.0f, wsum = 0.0f;
    for (int i = 0; i < np; i++) {
        const float w = wt[i];
        sxx += w * kx[i] * kx[i];
        sxy += w * kx[i] * ky[i];
        syy += w * ky[i] * ky[i];
        bx  += w * kx[i] * t[i];
        by  += w * ky[i] * t[i];
        wsum += w;
    }
    const float det = sxx * syy - sxy * sxy;
    if (fabsf(det) < 1e-6f) return 0;

    const float X = ( syy * bx - sxy * by) / det;
    const float Y = (-sxy * bx + sxx * by) / det;

    r->aperture = sqrtf(X * X + Y * Y);
    r->az_deg   = atan2f(Y, X) * (180.0f / 3.14159265f);
    r->npair    = np;

    float qmin = 1e9f;
    for (int i = 0; i < np; i++) if (qq[i] < qmin) qmin = qq[i];
    r->qmin = qmin;

    /* 加权归一化残差。np==2 时没有冗余，残差恒为 0，不能当判据用。 */
    if (np > 2) {
        float ss = 0.0f;
        for (int i = 0; i < np; i++) {
            const float d = kx[i] * X + ky[i] * Y - t[i];
            ss += wt[i] * d * d;
        }
        r->resid = (r->aperture > 1e-3f && wsum > 1e-6f)
                 ? sqrtf(ss / wsum / (float)(np - 2)) / r->aperture
                 : 1.0f;
    }
    return 1;
}

/*============================================================================
 * 五、GPIO / BRAM 基础操作
 *==========================================================================*/
/* 第二块缓冲区相对第一块的字节偏移；0 = PL 只写一块缓冲。
 * 由 pl_probe_layout() 在上电时实测决定。 */
static u32 pl_bank_bytes = 0;

/*============================================================================
 * 五之一、PL 帧格式自检（上电只跑一次）
 *
 * 为什么需要这一步：
 *   板上位流和 RTL 源码并不一致 —— 实测 PL 每帧只写 256 点（2 KB），
 *   而且并没有把第二块缓冲区放在 RTL 里写的 8 KB 处。
 *   PS 若仍按"1024 点 + 8 KB 双箱"去读，每帧里就有 75% 是零；
 *   GCC-PHAT 的互相关会被这 75% 的零死死钉在 lag=0，
 *   时延恒等于 0，方位角自然怎么算都不对、页面永远"低置信"。
 *
 * 做法：把 16 KB 全填成 0xA5A5A5A5，陪 PL 走几帧（只同步账本、不搬数据，
 *   给 PL 腾出空位继续写），再看哪几块 2 KB 被真正改写过。
 *   改写的那块就是它当前在用的缓冲区。这样既不用改位流，
 *   将来把位流修好（真 1024 点 + 8 KB 双箱）后，这份代码也能自己认出来。
 *==========================================================================*/
#define PL_PROBE_PAT    0xA5A5A5A5u
#define PL_PROBE_BLOCKS 8            /* 16 KB / 2 KB */

static void pl_probe_layout(void)
{
    volatile u32 *p = (volatile u32 *)BRAM_BASE;
    int changed[PL_PROBE_BLOCKS];

    for (int i = 0; i < 4096; i++) p[i] = PL_PROBE_PAT;   /* 16 KB 铺底 */

    /* 陪 PL 走 40 个握手节拍：只更新 rd_cnt，不搬数据 */
    for (int k = 0; k < 40; k++) {
        u32 wr = XGpio_DiscreteRead(&Gpio, 1) & 0x3u;
        XGpio_DiscreteWrite(&Gpio, 2, wr);
        for (volatile int d = 0; d < 150000; d++) { }
    }

    for (int b = 0; b < PL_PROBE_BLOCKS; b++) changed[b] = 0;
    for (int i = 0; i < 4096; i++)
        if (p[i] != PL_PROBE_PAT) changed[i >> 9]++;       /* 512 字 = 2 KB */

    /* 重新对齐账本，丢弃探测期间写的东西 */
    rd_cnt = XGpio_DiscreteRead(&Gpio, 1) & 0x3u;
    XGpio_DiscreteWrite(&Gpio, 2, rd_cnt);

    if      (changed[1] > 64) pl_bank_bytes = 2048u;      /* 256 点 x 8 字节 */
    else if (changed[2] > 64) pl_bank_bytes = 4096u;
    else if (changed[4] > 64) pl_bank_bytes = 8192u;
    else                      pl_bank_bytes = 0u;         /* 只有一块缓冲   */

    xil_printf("PL frame probe: blk0=%d blk1=%d blk2=%d blk4=%d -> bank offset=%u B\r\n",
               changed[0], changed[1], changed[2], changed[4],
               (unsigned)pl_bank_bytes);
}

static int handshake_init(void)
{
    XGpio_Config *cfg = XGpio_LookupConfig(GPIO_BASE);
    if (cfg == NULL) return -1;                      /* SDT 流程：按基地址找 */
    if (XGpio_CfgInitialize(&Gpio, cfg, cfg->BaseAddress) != XST_SUCCESS) return -1;

    XGpio_SetDataDirection(&Gpio, 1, 0xFFFFFFFFu);   /* 通道1 全输入(读PL)   */
    XGpio_SetDataDirection(&Gpio, 2, 0x00000000u);   /* 通道2 全输出(写PL)   */

    /* 上电对齐（很重要，别删）：
     *   PL 在 PS 跑起来之前就已经自由采样了，它的 wr_cnt 可能已经走到 1 或 2。
     *   如果这里仍然从 rd_cnt = 0 开始，PS 就会去读"这个位置可能从来没写过"
     *   的箱 —— 读出来是上电初值（全 0，也就是 -90 dBFS），
     *   典型症状就是"一帧有声、一帧全零"地来回跳。
     *   把 rd_cnt 直接对到 PL 当前的 wr_cnt，等于宣布"你之前写的我都不读了"，
     *   从这一刻起两边账本严格同步，第一次读到的必然是完整新箱。
     *   连读两次是为了避开 wr_cnt 正在翻转的那一拍。 */
    rd_cnt = XGpio_DiscreteRead(&Gpio, 1) & 0x3u;
    XGpio_DiscreteWrite(&Gpio, 2, rd_cnt);
    rd_cnt = XGpio_DiscreteRead(&Gpio, 1) & 0x3u;
    XGpio_DiscreteWrite(&Gpio, 2, rd_cnt);

    xil_printf("sync ok: PS rd_cnt = PL wr_cnt = %u\r\n", (unsigned)rd_cnt);
    return 0;
}

/* 等待 PL 装满新的一箱，然后把它搬进 ch[4][]，立刻回报 rd_cnt。
 * 返回 0 成功。注意"读完立刻记账"——让 PL 尽快恢复采集，计算放在后面。 */
static int grab_frame(void)
{
    /* 轮询：wr_cnt == rd_cnt 说明没有新箱，接着等（不要在这里加延时！） */
    while (1) {
        u32 wr = XGpio_DiscreteRead(&Gpio, 1) & 0x3u;
        if (wr != rd_cnt) break;
    }

    u32 bank = rd_cnt & 1u;
    /* 用上电实测出来的偏移，而不是硬写 8 KB —— 见 pl_probe_layout() */
    u32 base = pl_bank_bytes ? (bank * pl_bank_bytes) : 0u;
    volatile u32 *p = (volatile u32 *)(BRAM_BASE + base);

    for (int i = 0; i < FRAME_LEN; i++) {
        u32 lo = p[2*i];                         /* {ch1, ch0} 低 32 位      */
        u32 hi = p[2*i + 1];                     /* {ch3, ch2} 高 32 位      */
        ch[0][i] = (s16)(lo & 0xFFFFu);          /* AXI 是小端，低 16 位在前 */
        ch[1][i] = (s16)(lo >> 16);
        ch[2][i] = (s16)(hi & 0xFFFFu);
        ch[3][i] = (s16)(hi >> 16);
    }

    rd_cnt = (rd_cnt + 1u) & 0x3u;               /* 记账（模 4 回绕）        */
    XGpio_DiscreteWrite(&Gpio, 2, rd_cnt);       /* ★ 立刻告诉 PL：读完了    */
    return 0;
}

/*============================================================================
 * 五之二、响度估计 —— 决定"这一箱到底要不要算方位"
 *   用 RMS 而不是峰值：拍手只有几毫秒，峰值一个毛刺就能骗过它；
 *   RMS 衡量的是"这一帧里总共有多少声能"，对毛刺和一个坏点都不敏感。
 *   计算量：4×1024 次乘加，约 0.1 ms，比后面 FFT 便宜两个数量级。
 *==========================================================================*/
static float noise_floor_db = FLOOR_INIT_DB;   /* 噪声本底（自适应）             */
static int   floor_inited   = 0;               /* 是否已用实测值初始化           */

/* 返回本帧响度，单位 dBFS（0 = 满量程 32768，-60 = 很安静） */
static float frame_level_dbfs(void)
{
    float e = 0.0f;
    for (int c = 0; c < 4; c++) {
        float m = 0.0f;
        for (int i = 0; i < FRAME_LEN; i++) m += (float)ch[c][i];
        m /= (float)FRAME_LEN;                       /* 这一路的直流偏置     */
        float s = 0.0f;
        for (int i = 0; i < FRAME_LEN; i++) {
            float v = (float)ch[c][i] - m;           /* 去掉 DC 再算功率     */
            s += v * v;
        }
        float p = s / (float)FRAME_LEN;              /* 这一路的平均功率     */
        e += p;
        float crms = sqrtf(p);
        if (crms < 1.0f) crms = 1.0f;
        ch_level_dbfs[c] = 20.0f * log10f(crms / 32768.0f);
    }
    e *= 0.25f;                                      /* 四路取平均           */

    float rms = sqrtf(e);
    if (rms < 1.0f) rms = 1.0f;                      /* 防 log10(0)          */
    return 20.0f * log10f(rms / 32768.0f);
}

/* 四舍五入到 0.1（返回值是"十倍值"，供 xil_printf 拆成整数打印用）。
 * xil_printf 不支持 %f，所以全项目统一用这个函数把 float 变整数。 */
/* 判定这一帧哪几路麦克风有信号。
 *   比最强那一路低 MIC_DEAD_MARGIN_DB 以上 -> 认为该路掉线
 *   （没插好 / 没供电 / L-R 脚没接地 / SD 线悬空 都是这个现象）
 *   四路都低于 MIC_DEAD_FLOOR_DB -> 房间本来就安静，不下结论，
 *   沿用上一次的判定，免得安静时页面误报通道故障。 */
static unsigned mic_alive_mask(void)
{
    static unsigned last = 0xFu;
    float hi = ch_level_dbfs[0];
    for (int c = 1; c < 4; c++)
        if (ch_level_dbfs[c] > hi) hi = ch_level_dbfs[c];
    if (hi < MIC_DEAD_FLOOR_DB) return last;
    unsigned m = 0;
    for (int c = 0; c < 4; c++)
        if (ch_level_dbfs[c] > hi - MIC_DEAD_MARGIN_DB) m |= 1u << c;
    last = m;
    return m;
}

static int round10(float v)
{
    return (int)(v * 10.0f + (v >= 0.0f ? 0.5f : -0.5f));
}

#if LEVEL_ONLY
/*============================================================================
 * 五之一、只输出响度（LEVEL_ONLY=1 时用）
 *   把 dBFS 画成 40 格 ASCII 电平条，映射 -80 dB（空）~ 0 dB（满）。
 *   顺带把 PL 的账本 wr_cnt 也打出来 —— 它是分辨"两个箱哪一半坏了"的关键：
 *
 *   · wr_cnt 一直不变（冻死）→ PL 根本没收到 PS 的"读完了"回执，
 *       写完第一箱就停机 → 查 AXI GPIO 通道2 是否用 gpio2_io_o 接到
 *       acq_top 的 rd_cnt_ps；
 *   · wr_cnt 正常 0→1→2→3 循环，但电平仍一响一零交替 → 第二个箱
 *       的数据通路断了 → 查 Address Editor 里 BRAM Range 是不是 16K、
 *       bram_addr[10]（箱号位）有没有接进 BMG 的 addrb。
 *==========================================================================*/
static void print_meter(float db, u32 wr, u32 rd)
{
    const int W = 40;
    int w = (int)((db + 80.0f) * (float)W / 80.0f + 0.5f);
    if (w < 0)  w = 0;
    if (w > W)  w = W;

    char bar[W + 1];
    for (int i = 0; i < W; i++) bar[i] = (i < w) ? '#' : '-';
    bar[W] = '\0';

    int d = (int)(db + (db >= 0.0f ? 0.5f : -0.5f));   /* 取整到 1 dB */
    xil_printf("lvl %4d dB  [%s]  wr=%u rd=%u\r\n", d, bar, wr, rd);
}
#endif

#if DIAG_MODE
/*============================================================================
 * 五之三、上板体检：把每一路的真实数值量出来（只在 DIAG_MODE=1 时用到）
 *
 *   为什么要看三个数而不是一个电平：
 *
 *   · lvl（电平）—— 有多响。安静房间应当 ≤ -55 dBFS。
 *   · zcr（过零率）—— 这一帧里"符号翻转"的比例。
 *        真实声音（说话、拍手、空调噪声）由于有低频成分，多集中在 3%~35%；
 *        而一根【悬空的 SD 线】收到的是纯随机比特，过零率必然接近 50%。
 *        这是区分"真声音"和"线没接好"最有力的一眼。
 *   · peak（峰值）—— 该帧绝对值的最大值。如果四路都在 32767 附近，
 *        说明不是"声音大"，而是数据在满量程上削顶/乱跳。
 *   · dc（直流偏置）—— INMP441 输出是 24 位补码、本身无直流，
 *        正常应当接近 0；如果稳定偏在几万，说明比特位没对齐。
 *==========================================================================*/
typedef struct {
    float lvl;      /* 等效电平 dBFS（用平均绝对值估，比 RMS 抗毛刺）*/
    int   zcr;      /* 过零率，百分比                                */
    int   peak;     /* 绝对值峰值                                    */
    int   pk_at;    /* 峰值出现的下标（每帧都一样 = 有结构性问题）    */
    int   zeros;    /* 恰好等于 0 的样本个数（后半帧全零会 = 512）    */
    float dc;       /* 直流偏置                                      */
} ch_stat_t;

static void ch_measure(const s16 *x, int n, ch_stat_t *st)
{
    float sum = 0.0f;
    for (int i = 0; i < n; i++) sum += (float)x[i];
    float dc = sum / (float)n;

    float a = 0.0f;
    int   z = 0, pk = 0, pk_at = 0, n0 = 0;
    for (int i = 0; i < n; i++) {
        a += fabsf((float)x[i] - dc);
        int av = (x[i] < 0) ? -x[i] : x[i];
        if (av > pk) { pk = av; pk_at = i; }
        if (x[i] == 0) n0++;
        if (i && ((x[i] >= 0) != (x[i - 1] >= 0))) z++;   /* 符号翻转计数 */
    }
    a /= (float)n;

    /* 平均绝对值 → 等效 RMS：高斯信号 rms ≈ 1.2533 × mean|x|
     * 用 mean|x| 而不是真 RMS，是因为【一根毛刺就能把 RMS 抬高几十 dB】，
     * 而 mean|x| 只按线性比例贡献，天生抗孤立坏点。 */
    float amp = 1.2533f * a;
    if (amp < 1.0f) amp = 1.0f;

    st->lvl   = 20.0f * log10f(amp / 32768.0f);
    st->zcr   = (n > 1) ? (z * 100 / (n - 1)) : 0;
    st->peak  = pk;
    st->pk_at = pk_at;
    st->zeros = n0;
    st->dc    = dc;
}

/* 主导频率：对一路做加窗 FFT，找幅度最大的谱线。
 * 返回频率 Hz，*pct = 该谱线占总功率的百分比（单音 ≈ 高，宽带 ≈ 低）。
 * 只在体检模式用（正常模式下 re0/im0 被 gcc_phat 占用）。 */
static float dominant_freq(const s16 *x, int n, int *pct)
{
    float mean = 0.0f;
    for (int i = 0; i < n; i++) mean += (float)x[i];
    mean /= (float)n;
    for (int i = 0; i < n; i++) {
        re0[i] = ((float)x[i] - mean) * hann_tab[i];
        im0[i] = 0.0f;
    }
    fft(re0, im0);

    int   best = 1;
    float bm = 0.0f, tot = 0.0f;
    for (int k = 1; k < n / 2; k++) {
        float m = re0[k] * re0[k] + im0[k] * im0[k];
        tot += m;
        if (m > bm) { bm = m; best = k; }
    }
    *pct = (tot > 0.0f) ? (int)(bm * 100.0f / tot) : 0;
    return (float)best * FS_HZ / (float)n;
}
#endif  /* DIAG_MODE */

/* 取 n 个数的中位数（就地插入排序，n 只有 16，忽略效率）。
 * 用中位数而不是平均值：个别帧是静音全零、或者刚好有人拍手，都带不偏结果。 */
static float median_of(float *a, int n)
{
    for (int i = 1; i < n; i++) {
        float v = a[i];
        int j = i - 1;
        while (j >= 0 && a[j] > v) { a[j + 1] = a[j]; j--; }
        a[j + 1] = v;
    }
    return a[n / 2];
}

/* 更新噪声本底：比本底低就快跟（FLOOR_FALL），比本底高就以 FLOOR_MAX_UP
 * 为上限慢慢爬。为什么要限速：如果不限，连续吵 1 秒（约 48 帧）本底就能
 * 爬到拍手电平上去，门限随之抬高，后面再拍手就不触发了 —— 也就是"越吵越聋"。
 * 限速 0.1 dB/帧 后，吵 1 秒本底最多升 4.8 dB，灵敏度基本不受影响；
 * 而房间真的变吵时，十几秒内也能跟上，不用手工重调参数。 */
static void floor_update(float lvl)
{
    if (!floor_inited) {                             /* 第一次：直接对齐实测 */
        noise_floor_db = lvl;
        floor_inited   = 1;
        return;
    }
    if (lvl < noise_floor_db) {
        noise_floor_db += (lvl - noise_floor_db) * FLOOR_FALL;   /* 快降 */
    } else {
        float up = (lvl - noise_floor_db) * FLOOR_RISE;           /* 慢升 */
        if (up > FLOOR_MAX_UP) up = FLOOR_MAX_UP;
        noise_floor_db += up;
    }

    if (noise_floor_db < -85.0f) noise_floor_db = -85.0f;  /* 别太离谱      */
    if (noise_floor_db > -40.0f) noise_floor_db = -40.0f;  /* 本底最高只认 -40，
                                                            * 否则门限会被抬到
                                                            * 拍手都够不着。   */
}

/* 当前生效的门限 = max(本底 + 余量, 绝对下限) */
static float gate_threshold(void)
{
    float thr = noise_floor_db + GATE_MARGIN_DB;
    return (thr < GATE_MIN_DB) ? GATE_MIN_DB : thr;
}

/*============================================================================
 * 五之五、方位角后处理：最近 3 个有效帧取中值
 *
 *   单帧 TDOA 的抖动来自两处：房间反射（回声让相关峰变形）、
 *   以及拍手本身不是理想脉冲。表现出来就是同一个方向连拍三下，
 *   角度能差十几度。取 3 点中值能把偶发的一帧野值整条扔掉，
 *   而中值不会被两端极值拉扯（平均值做不到这一点）。
 *
 *   只接受"一致性自检通过"的帧；一旦环境安静下来就把历史清空，
 *   免得上一次拍手的方向污染下一次。
 *==========================================================================*/
static float az_hist[3];        /* 最近几个方位角                */
static int   az_hist_n = 0;     /* 已经攒了几个（最多 3）         */
static int   az_hist_p = 0;     /* 下一个该写哪个格子             */

static void az_hist_reset(void) { az_hist_n = 0; az_hist_p = 0; }

static float az_smooth(float v)
{
    az_hist[az_hist_p] = v;
    az_hist_p = (az_hist_p + 1) % 3;
    if (az_hist_n < 3) az_hist_n++;

    float t[3];
    int   n = az_hist_n;
    for (int i = 0; i < n; i++) t[i] = az_hist[i];
    for (int i = 0; i < n - 1; i++)
        for (int j = i + 1; j < n; j++)
            if (t[j] < t[i]) { float x = t[i]; t[i] = t[j]; t[j] = x; }

    return t[n / 2];
}

/*============================================================================
 * 六、主函数
 *==========================================================================*/
/*============================================================================
 * 五之六、屏幕/灯环控制（PL 硬件引擎渲染，PS 只写寄存器）
 *==========================================================================*/
static int g_led_state = -1;

static int loudest_channel(void)
{
    int best = 0, c, i;
    float best_rms = -1.0f;
    for (c = 0; c < 4; c++) {
        float m = 0.0f, s = 0.0f;
        for (i = 0; i < FRAME_LEN; i++) m += (float)ch[c][i];
        m /= (float)FRAME_LEN;
        for (i = 0; i < FRAME_LEN; i++) { float v = (float)ch[c][i] - m; s += v * v; }
        s = sqrtf(s / (float)FRAME_LEN);
        if (s > best_rms) { best_rms = s; best = c; }
    }
    return best;
}

static float zc_freq(const s16 *x)
{
    int z = 0, i;
    for (i = 1; i < FRAME_LEN; i++)
        if ((x[i] >= 0) != (x[i - 1] >= 0)) z++;
    return (float)z / (2.0f * (float)(FRAME_LEN - 1)) * FS_HZ;
}

/* 人声分类只使用 1024 点频谱的前 256 条正频线（约 12.2 kHz）。
 * 这里不改变声音/定位数据，只在“已过响度门限”的帧上给状态机一个人声判据。 */
#define SPEECH_POWER_BINS 256
#define SPEECH_LOW_MAX_BIN 10
static float speech_prev_power[SPEECH_POWER_BINS + 1];
static int   speech_prev_valid = 0;

/* 7 帧约 147 ms 的平滑窗。原始分类偶发低于阈值时，不让人声累计
 * 与“其他声音”累计来回抵消；进入 62、退出 48 形成回差。 */
#define SPEECH_CONF_WINDOW 7
static unsigned char speech_conf_hist[SPEECH_CONF_WINDOW];
static int speech_hist_n = 0;
static int speech_hist_p = 0;
static int speech_hold = 0;

static void speech_spectrum_reset(void)
{
    speech_prev_valid = 0;
}

static void speech_smoothing_reset(void)
{
    speech_hist_n = 0;
    speech_hist_p = 0;
    speech_hold = 0;
    speech_spectrum_reset();
}

static int speech_confidence_debounce(int raw)
{
    int i, sum = 0;
    speech_conf_hist[speech_hist_p] = (unsigned char)raw;
    speech_hist_p = (speech_hist_p + 1) % SPEECH_CONF_WINDOW;
    if (speech_hist_n < SPEECH_CONF_WINDOW) speech_hist_n++;
    for (i = 0; i < speech_hist_n; ++i) sum += speech_conf_hist[i];
    int avg = sum / speech_hist_n;
    if (speech_hold) {
        if (avg < 48) speech_hold = 0;
    } else if (avg >= 60) {
        speech_hold = 1;
    }
    return avg;
}

static int classify_speech_frame(const s16 *x, float lvl, float *out_zf)
{
    int i, k, z = 0;
    float mean = 0.0f;
    for (i = 0; i < FRAME_LEN; i++) mean += (float)x[i];
    mean /= (float)FRAME_LEN;

    for (i = 0; i < FRAME_LEN; i++) {
        re0[i] = ((float)x[i] - mean) * hann_tab[i];
        im0[i] = 0.0f;
        if (i && ((x[i] >= 0) != (x[i - 1] >= 0))) z++;
    }
    fft(re0, im0);

    float total = 0.0f, log_sum = 0.0f, weighted = 0.0f;
    float low = 0.0f, flux = 0.0f, peak = 0.0f;
    for (k = 1; k <= SPEECH_POWER_BINS; k++) {
        float p = re0[k] * re0[k] + im0[k] * im0[k];
        total += p;
        weighted += (float)k * p;
        if (k <= SPEECH_LOW_MAX_BIN) low += p;
        if (p > peak) peak = p;
        log_sum += logf(p > 1.0e-12f ? p : 1.0e-12f);
        if (speech_prev_valid) {
            float d = p - speech_prev_power[k];
            if (d > 0.0f) flux += d;
        }
        speech_prev_power[k] = p;
    }
    speech_prev_valid = 1;

    qz_speech_features_t f;
    float mean_power = total / (float)SPEECH_POWER_BINS;
    f.flatness = (total > 0.0f) ?
        expf(log_sum / (float)SPEECH_POWER_BINS) / mean_power : 0.0f;
    f.centroid_hz = (total > 0.0f) ?
        (weighted / total) * (FS_HZ / (float)FRAME_LEN) : 0.0f;
    f.flux = (total > 0.0f) ? flux / total : 0.0f;
    f.lowratio = (total > 0.0f) ? low / total : 0.0f;
    f.peak_ratio = (total > 0.0f) ? peak / total : 0.0f;
    f.zcr_pct = (float)z * 100.0f / (float)(FRAME_LEN - 1);

    /* round10(x) 返回 x*10；先乘 100、100 Hz 再调用，得到比例×1000、
     * Hz 整数和过零百分比×10，避免重复放大一个数量级。 */
    g_feat_flat1000 = round10(f.flatness * 100.0f);
    g_feat_centroid_hz = round10(f.centroid_hz * 0.1f);
    g_feat_flux1000 = round10(f.flux * 100.0f);
    g_feat_low1000 = round10(f.lowratio * 100.0f);
    g_feat_peak1000 = round10(f.peak_ratio * 100.0f);
    g_feat_zcr10 = round10(f.zcr_pct);

    float zf = (float)z / (2.0f * (float)(FRAME_LEN - 1)) * FS_HZ;
    if (out_zf) *out_zf = zf;
    (void)lvl;
    return qz_speech_confidence(&f);
}

/* 三个有效区域使用固定参数，噪音进入更高状态后才会升档：
 * QUIET 常亮；WARN 0.25 Hz；ALARM 0.35 Hz。三角波给出连续、低开销的呼吸曲线。 */
static int led_brightness_pct(int state)
{
    int p, tri, lo, hi;

    switch (state) {
        case ST_QUIET: return (int)LED_QUIET_PCT;
        case ST_WARN:  lo = (int)LED_WARN_MIN_PCT;  hi = (int)LED_WARN_MAX_PCT;  break;
        case ST_ALARM: lo = (int)LED_ALARM_MIN_PCT; hi = (int)LED_ALARM_MAX_PCT; break;
        default:       return (int)LED_IDLE_PCT;
    }

    p = (int)(led_phase >> 8);
    tri = (p < 128) ? p : (255 - p);       /* 0..127..0 */
    return lo + (((hi - lo) * tri) / 127);
}

static void led_tick(int state)
{
    u32 color;

    if (state != g_led_state) {            /* 状态切换从该档最低亮度起呼吸 */
        g_led_state = state;
        led_phase = 0;
    }

    color = led_scale_grb(led_color_for_state(state),
                          (u32)led_brightness_pct(state));
    led_update_toggle ^= 1u;               /* 上升沿隔帧出现，灯环约 24 Hz 刷新 */
    display_update_led(color, (int)led_update_toggle);

    if (state == ST_WARN)       led_phase += LED_BREATH_WARN_PHASE_STEP;
    else if (state == ST_ALARM) led_phase += LED_BREATH_ALARM_PHASE_STEP;
}

static void display_tick(float lvl, float zf, int state)
{
    float spl = lvl + 120.0f - a_weight(zf);
    int fill = (int)((spl - 25.0f) * 2.0f);
    if (fill < 0) fill = 0;
    if (fill > 100) fill = 100;
    int db10 = (int)(spl * 10.0f);
    if (db10 < 0) db10 = 0;
    int refresh = (display_hold_frames == 0);
    if (++display_hold_frames >= DISPLAY_REFRESH_FRAMES)
        display_hold_frames = 0;

    g_display_state = state;
    g_display_spl10 = db10;
    if (refresh) display_update_screen(fill, state, db10);  /* 屏幕限速 1 Hz */
    led_tick(state);                                        /* 灯环独立呼吸刷新 */
}

/* UART 机器可读接口。算法只把既有结果交到这里，此函数不修改门控或方位。
 *
 * 一行格式：
 * QZ,kind=<0静默心跳|1定位帧|2时延越界>,seq=...,az10=...,spl10=...,state=...,
 *    trust=...,t02_100=...,t13_100=...,thr10=...,floor10=...,bad=...,
 *    wr=...,rd=...,r0_10=...,r1_10=...,r2_10=...,r3_10=...
 * 所有 *10 字段的单位均为 0.1；t02_100/t13_100 为 0.01 采样点。 */
static void telemetry_line(u32 seq, int kind, int az10, int trust,
                           float t02, float t13, float thr, u32 bad)
{
    /* 注意：round10() 自己就乘了 10，这里要用 lroundf，
     * 否则 t02_100 会被多放大 10 倍，页面上的时延就变成"10 倍采样点"。 */
    int t02_100 = (int)lroundf(t02 * 100.0f);
    int t13_100 = (int)lroundf(t13 * 100.0f);
    int thr10 = round10(thr);
    int flr10 = round10(noise_floor_db);
    u32 wr = XGpio_DiscreteRead(&Gpio, 1) & 0x3u;
    int r0 = round10(ch_level_dbfs[0]);
    int r1 = round10(ch_level_dbfs[1]);
    int r2 = round10(ch_level_dbfs[2]);
    int r3 = round10(ch_level_dbfs[3]);

    /* 奇偶序号构成轻量 seqlock：奇数表示正在写，偶数表示快照完整。 */
    g_qz_mem_telemetry.counter++;
    g_qz_mem_telemetry.kind = kind;
    g_qz_mem_telemetry.seq = (s32)seq;
    g_qz_mem_telemetry.az10 = az10;
    g_qz_mem_telemetry.spl10 = g_display_spl10;
    g_qz_mem_telemetry.state = g_display_state;
    /* trust 字段打包（bit0 与旧版兼容，其余是新增的诊断位）：
     *   bit0      本帧自检通过
     *   [7:4]     有效麦克风掩码，bit i = CHi 有信号
     *   [15:8]    三麦最小二乘归一化残差 ×100（四麦模式恒 0）
     *   [23:16]   实测孔径 A/TAU_SIDE ×100（255 饱和）
     * 旧版主机只读 bit0，所以这个打包对老页面是安全的。 */
    {
        int rs100 = (int)(g_last_resid * 100.0f + 0.5f);
        int ap100 = (int)(g_last_ap    * 100.0f + 0.5f);
        if (rs100 < 0) rs100 = 0;
        if (rs100 > 255) rs100 = 255;
        if (ap100 < 0) ap100 = 0;
        if (ap100 > 255) ap100 = 255;
        g_qz_mem_telemetry.trust =
            (s32)(((u32)(trust ? 1 : 0))
                | (((u32)g_mic_mask & 0xFu) << 4)
                | ((u32)rs100 << 8)
                | ((u32)ap100 << 16));
    }
    g_qz_mem_telemetry.t02_100 = t02_100;
    g_qz_mem_telemetry.t13_100 = t13_100;
    g_qz_mem_telemetry.thr10 = thr10;
    g_qz_mem_telemetry.floor10 = flr10;
    g_qz_mem_telemetry.bad = (s32)bad;
    g_qz_mem_telemetry.wr = (s32)wr;
    g_qz_mem_telemetry.rd = (s32)rd_cnt;
    g_qz_mem_telemetry.r0_10 = r0;
    g_qz_mem_telemetry.r1_10 = r1;
    g_qz_mem_telemetry.r2_10 = r2;
    g_qz_mem_telemetry.r3_10 = r3;
    g_qz_mem_telemetry.speech_conf = g_speech_conf;
    g_qz_mem_telemetry.speech_eff = g_speech_eff;
    g_qz_mem_telemetry.speech_state = g_speech_state;
    g_qz_mem_telemetry.flat1000 = g_feat_flat1000;
    g_qz_mem_telemetry.centroid_hz = g_feat_centroid_hz;
    g_qz_mem_telemetry.flux1000 = g_feat_flux1000;
    g_qz_mem_telemetry.low1000 = g_feat_low1000;
    g_qz_mem_telemetry.peak1000 = g_feat_peak1000;
    g_qz_mem_telemetry.zcr10 = g_feat_zcr10;
    g_qz_mem_telemetry.speech_ms = (s32)g_speech_ms;
    g_qz_mem_telemetry.other_ms = (s32)g_other_ms;
    g_qz_mem_telemetry.below_ms = (s32)g_below_ms;
    g_qz_mem_telemetry.counter++;
    /* CPU 运行时 JTAG 读取的是物理内存；每帧刷新两条 cache line，
     * 让主机拿到完整快照，避免后半段低 32 位仍停留在旧缓存。 */
    Xil_DCacheFlushRange((INTPTR)&g_qz_mem_telemetry,
                         (u32)sizeof(g_qz_mem_telemetry));

    xil_printf("QZ,kind=%d,seq=%u,az10=%d,spl10=%d,state=%d,trust=%d,"
               "t02_100=%d,t13_100=%d,thr10=%d,floor10=%d,bad=%u,"
               "wr=%u,rd=%u,r0_10=%d,r1_10=%d,r2_10=%d,r3_10=%d,"
               "clsconf=%d,clseff=%d,spklass=%d,flat1000=%d,centroid_hz=%d,"
               "flux1000=%d,low1000=%d,peak1000=%d,zcr10=%d,"
               "speech_ms=%u,other_ms=%u,below_ms=%u\r\n",
               kind, (unsigned)seq, az10, g_display_spl10, g_display_state,
               trust, t02_100, t13_100, thr10, flr10, (unsigned)bad,
               (unsigned)wr, (unsigned)rd_cnt, r0, r1, r2, r3,
               g_speech_conf, g_speech_eff, g_speech_state,
               g_feat_flat1000, g_feat_centroid_hz, g_feat_flux1000,
               g_feat_low1000, g_feat_peak1000, g_feat_zcr10,
               g_speech_ms, g_other_ms, g_below_ms);
}

int main(void)
{
    /*----------------------------------------------------------------------*/
    /* 一、启动横幅 —— 上板第一眼就看这里，先确认版本号对不对               */
    /*----------------------------------------------------------------------*/
    xil_printf("\r\n==================================================\r\n");
    xil_printf(" Sound Source Locator   [%s]\r\n", BUILD_TAG);
    xil_printf(" square array 50 mm   fs=%d Hz   frame=%d\r\n",
               (int)FS_HZ, FRAME_LEN);
    xil_printf("--------------------------------------------------\r\n");
#if LEVEL_ONLY
    xil_printf(" MODE: loudness only  (no angle, no gating)\r\n");
    xil_printf("==================================================\r\n");
#else
    xil_printf(" How to read a line:\r\n");
    xil_printf("   #0012  AZ =  41.8 deg   lvl = -19.3 dB\r\n");
    xil_printf("\r\n");
    xil_printf("   #0012  frame number (1 frame = 21 ms)\r\n");
    xil_printf("   AZ     direction the sound came from\r\n");
    xil_printf("          0 east / 90 north / 180 west / -90 south\r\n");
    xil_printf("          (counter-clockwise from +X)\r\n");
    xil_printf("   lvl    loudness of that frame, dBFS\r\n");
    xil_printf("          -20 clap, -45 quiet office, -65 bedroom\r\n");
    xil_printf("   ?      TDOA looks inconsistent (low trust)\r\n");
    xil_printf("\r\n");
    xil_printf(" Quiet frames print NOTHING. Silent port = good.\r\n");
    xil_printf("==================================================\r\n");
#endif

#if DIAG_MODE
    xil_printf("\r\n*** DIAG MODE: raw channel check, no gating, no AZ ***\r\n");
    xil_printf(" Every 10 frames you get one line with, for each of the\r\n");
    xil_printf(" four channels:  lvl (dBFS) | zcr (%%) | peak (counts)\r\n");
    xil_printf("\r\n");
    xil_printf(" How to tell a broken input:\r\n");
    xil_printf("   zcr ~50%% and lvl > -35  -> pure random bits, i.e. that\r\n");
    xil_printf("        SD line floats: mic not powered, SD not on the pin,\r\n");
    xil_printf("        or L/R is not tied to GND.\r\n");
    xil_printf("   lvl below -85 (all zeros) -> nothing arrives at all.\r\n");
    xil_printf("   zcr 2..35%% and lvl -70..-40 -> REAL audio, path is fine.\r\n");
    xil_printf("\r\n");
    xil_printf(" Loopback test (proves pin + XDC + BRAM are OK):\r\n");
    xil_printf("   unplug SD0 from the mic, touch GND -> ch0 reads 0\r\n");
    xil_printf("   touch 3V3                          -> ch0 reads -1\r\n");
    xil_printf("==================================================\r\n");
#endif

    fft_init();                                  /* 旋转因子 + 汉宁窗只算一次 */

    if (handshake_init() != 0) {
        xil_printf("!! AXI GPIO init FAILED (check bitstream)\r\n");
        while (1);                               /* 平台里没有 AXI GPIO？别跑了 */
    }

    /* 实测 PL 的帧格式（每帧点数 / 第二箱偏移），再开始搬数据。
     * 这一步会把 16 KB 铺一遍测试图样，必须在标定之前做。 */
    pl_probe_layout();

    /*----------------------------------------------------------------------*/
    /* 二、上电标定：先吃 16 帧摸清环境本底，这一小段（0.35 秒）不输出        */
    /*----------------------------------------------------------------------*/
    static float cal_buf[CAL_FRAMES];
    for (int i = 0; i < CAL_FRAMES; i++) {
        grab_frame();
        cal_buf[i] = frame_level_dbfs();
    }
    noise_floor_db = median_of(cal_buf, CAL_FRAMES);
    floor_inited   = 1;

    {
        int f10 = round10(noise_floor_db);
        int g10 = round10(gate_threshold());
        xil_printf("calib %d frames: noise floor = %d.%d dBFS\r\n",
                   CAL_FRAMES, f10/10, (f10<0?-f10:f10)%10);
#if !LEVEL_ONLY
        xil_printf("                 gate opens at %d.%d dBFS\r\n",
                   g10/10, (g10<0?-g10:g10)%10);

        /* 两句诊断提示：本底异常高或异常低，基本都是硬件/接线问题 */
        if (noise_floor_db > -35.0f)
            xil_printf("!! ambient is very loud: mics OK? L/R really tied to GND?\r\n");
        if (noise_floor_db < -84.0f)
            xil_printf("!! ambient is digital silence: PL still writing zeros?\r\n");
#else
        (void)g10;
        xil_printf(" quiet room should read about -70 .. -55 dBFS\r\n");
        xil_printf(" meter below, one line per 0.1 s\r\n");
#endif
    }
#if !LEVEL_ONLY
    xil_printf("ready, waiting for a loud sound (clap)...\r\n");
#endif

    u32 frame_no = 0, bad_frames = 0;
    int loud_run = 0;                            /* 连续"响"了多少帧          */
    qz_state_machine_t qz_fsm;
    qz_state_reset(&qz_fsm);
    g_display_state = ST_QUIET;

    while (1) {
        grab_frame();                            /* ① 拿一箱（阻塞等新数据） */

#if LEVEL_ONLY
        /* 只输出响度：每 5 帧（≈0.1 秒）打一行电平条，别的什么都不算。
         * 每 5 帧而不是每帧，是为了让串口不刷屏、又能在拍手时肉眼看到跳变。 */
        if (frame_no % 5 == 0) print_meter(frame_level_dbfs());
        frame_no++;
        continue;
#endif

#if DIAG_MODE
        /*--------------------------------------------------------------
         * 体检模式：不看门限、不算方位，每 10 帧打印四路实测数值。
         * ★ 这里刻意放在门限判断【之前】，所以无论多安静都会打，
         *   目的是让"数据到底长什么样"无处可藏。
         *------------------------------------------------------------*/
        if (frame_no % 10 == 0) {
            ch_stat_t st[4];
            for (int c = 0; c < 4; c++) ch_measure(ch[c], FRAME_LEN, &st[c]);

            /* 一行看四路：电平 / 过零率 / 峰值@下标 / 恰好为0的样本数 */
            xil_printf("[chk]"
                       " c0 %4ddB z%2d pk%5d@%3d n0%4d"
                       " | c1 %4ddB z%2d pk%5d@%3d n0%4d"
                       " | c2 %4ddB z%2d pk%5d@%3d n0%4d"
                       " | c3 %4ddB z%2d pk%5d@%3d n0%4d\r\n",
                       (int)st[0].lvl, st[0].zcr, st[0].peak, st[0].pk_at, st[0].zeros,
                       (int)st[1].lvl, st[1].zcr, st[1].peak, st[1].pk_at, st[1].zeros,
                       (int)st[2].lvl, st[2].zcr, st[2].peak, st[2].pk_at, st[2].zeros,
                       (int)st[3].lvl, st[3].zcr, st[3].peak, st[3].pk_at, st[3].zeros);

            /* 每 30 帧：① 波形原样抖出来（跨过下标 512 的 16 个点）
             *   —— 是正弦、方波、毛刺，还是"前半有数后半全零"，一眼定论；
             * ② 主导频率 —— 若每帧都锁死在某个频率上（比如 3052 Hz =
             *   fs/16），说明数据里有周期性结构，指向时序/位对齐问题。 */
            if (frame_no % 30 == 0) {
                xil_printf("[wav] ch0[504..519]:");
                for (int i = 504; i < 520; i++) xil_printf(" %d", ch[0][i]);
                xil_printf("\r\n[wav] ch1[504..519]:");
                for (int i = 504; i < 520; i++) xil_printf(" %d", ch[1][i]);
                xil_printf("\r\n");

                int p0, p1;
                float f0 = dominant_freq(ch[0], FRAME_LEN, &p0);
                float f1 = dominant_freq(ch[1], FRAME_LEN, &p1);
                xil_printf("[frq] ch0 %5d Hz (p%d%%)  ch1 %5d Hz (p%d%%)\r\n",
                           (int)f0, p0, (int)f1, p1);
            }

            /* 第 10 帧做一次自动判读，直接把结论说出来 */
            if (frame_no == 10) {
                int same = 1, mx_lvl = -999;
                for (int i = 0; i < FRAME_LEN; i++) {
                    if (ch[0][i] != ch[1][i] || ch[0][i] != ch[2][i]
                        || ch[0][i] != ch[3][i]) { same = 0; break; }
                }
                for (int c = 0; c < 4; c++)
                    if ((int)st[c].lvl > mx_lvl) mx_lvl = (int)st[c].lvl;

                if (same)
                    xil_printf("!! 4 channels bit-identical -> same net driving all,"
                               " or SD pins mapped to one pin\r\n");
                else if (st[0].zeros > 400 && st[1].zeros > 400
                         && st[2].zeros > 400 && st[3].zeros > 400)
                    xil_printf("!! big zero region in every frame -> PL wrote only"
                               " part of the frame: check BRAM Range vs 16KB\r\n");
                else if (mx_lvl < -84)
                    xil_printf("!! all zeros -> no data at all: check mic VDD(3V3),"
                               " WS(48k), SCK(3.1M)\r\n");
                else if (st[0].zcr >= 42 && st[1].zcr >= 42
                         && st[2].zcr >= 42 && st[3].zcr >= 42 && mx_lvl > -35)
                    xil_printf("!! white noise on all 4 lines -> SD floating:"
                               " mic unpowered / SD off-pin / L-R not to GND\r\n");
                else if (mx_lvl > -20)
                    xil_printf("!! loud low-freq garbage on all lines -> see"
                               " [wav]/[frq] lines: bit alignment or float line\r\n");
                else
                    xil_printf("-- levels look like real audio; data path OK\r\n");
            }
        }
        frame_no++;
        continue;
#endif

#if DEBUG_RAW    /* 只看数据链路时打开；正常跑请关掉，否则安静时也会刷屏 */
        if (frame_no % 20 == 0) {
            xil_printf("raw ch0..3 @256: %5d %5d %5d %5d\r\n",
                       ch[0][256], ch[1][256], ch[2][256], ch[3][256]);
        }
#endif

        /* ② 量响度，决定这一帧算不算 --------------------------------------
         *  ★ 顺序很重要：先用【当前】本底算出门限并判断，再更新本底。
         *    反过来的话，拍手自己会把门限抬上去，出现"越拍越不触发"。 */
        float lvl = frame_level_dbfs();
        float thr = gate_threshold();
        int   loud = (lvl > thr);

        int lc = loudest_channel();
        float zf = zc_freq(ch[lc]);
        int speech = 0;
        int speech_eff = 0;
        if (loud) {
            speech = classify_speech_frame(ch[lc], lvl, &zf);
            speech_eff = speech_confidence_debounce(speech);
        } else {
            speech_spectrum_reset();
        }
        int state = qz_state_update(&qz_fsm, loud, speech_hold, 21u);
        if (!loud && qz_fsm.below_ms >= QZ_ACTIVE_GAP_GRACE_MS)
            speech_smoothing_reset();
        g_speech_conf = speech;
        g_speech_eff = speech_eff;
        g_speech_state = speech_hold;
        g_speech_ms = qz_fsm.speech_ms;
        g_other_ms = qz_fsm.other_ms;
        g_below_ms = qz_fsm.below_ms;
        floor_update(lvl);

        int l10 = round10(lvl);
        display_tick(lvl, zf, state);           /* 每帧更新色环 + 灯环 */

        if (!loud) {
            /* 环境噪声级：整帧丢弃 —— 不算方位、不打印任何方位数据。
             * VERBOSE_QUIET=1 时打一行电平，只用来标定门限，正式跑要关掉。 */
#if VERBOSE_QUIET
            if (frame_no % 20 == 0) {
                int t10 = round10(thr), f10 = round10(noise_floor_db);
                xil_printf("quiet   lvl=%d.%d  thr=%d.%d  floor=%d.%d dBFS\r\n",
                           l10/10, (l10<0?-l10:l10)%10,
                           t10/10, (t10<0?-t10:t10)%10,
                           f10/10, (f10<0?-f10:f10)%10);
            }
#endif
            if (frame_no % 20 == 0)
                telemetry_line(frame_no, 0, 0, 0, 0.0f, 0.0f, thr, bad_frames);
            loud_run = 0;                        /* 安静了，这次的"响"结束   */
            az_hist_reset();                     /* 方位历史也清空，下一声从头来 */
            frame_no++;
            continue;
        }

        /* ②' 持续大声限流：一次"响"最多打印 BURST_MAX_FRAMES 帧，
         *     超过就认定是持续噪声（空调、说话），等它安静下来再重新计。 */
        if (++loud_run > BURST_MAX_FRAMES) {
            /* 持续大声时仍然定期发静默心跳，避免网页把有效的四路电平
             * 和屏幕状态误判成串口断流。该分支不执行 GCC-PHAT，也不改变
             * 原有的限流和门控行为。 */
            if (frame_no % 20 == 0)
                telemetry_line(frame_no, 0, 0, 0, 0.0f, 0.0f, thr, bad_frames);
            frame_no++;
            continue;
        }

        /* ③ 够响了（拍手）：先判哪几路有信号，再用"能用的那几路"求时延。
         *
         *   四路全好   -> 仍旧走两条对角线（0↔2、1↔3），结果与旧版一致
         *   坏掉一路   -> 换三麦最小二乘：三条边 3 个方程解 2 个未知量，
         *                 多出来的那个方程正好当自检残差
         *   坏两路以上 -> 方程不够，直接报"通道故障"，绝不编一个角度出来 */
        unsigned mask = mic_alive_mask();
        g_mic_mask = mask;

        doa_result_t dr;
        if (!doa_solve(mask, &dr)) {
            bad_frames++;
            g_last_resid = 0.0f;
            g_last_ap    = 0.0f;
            telemetry_line(frame_no, 3, 0, 0, 0.0f, 0.0f, thr, bad_frames);
            if (frame_no % 4 == 0)
                xil_printf("#%04u  (skipped: %d mic alive, mask=0x%X)\r\n",
                           frame_no, doa_count(mask), mask);
            frame_no++;
            continue;
        }

        float t02 = dr.t_pair[0];       /* 第一对：0↔2（四麦/三麦都用它）   */
        float t13 = dr.t_pair[1];       /* 第二对：1↔3，或 0↔3（CH1 坏时）  */
        g_last_resid = dr.resid;
        g_last_ap    = dr.aperture / TAU_SIDE;
        g_qz_dbg[0]  = (u32)frame_no;
        g_qz_dbg[1]  = ((u32)dr.npair << 8) | (mask & 0xFFu);
        for (int i = 0; i < 6; i++) {
            int v = (int)(g_dbg_lag[i] * 100.0f);
            g_qz_dbg[2 + i] = (u32)v;
        }
        g_qz_dbg[8]  = (u32)(int)(dr.resid * 100.0f);
        g_qz_dbg[9]  = (u32)(int)(g_last_ap * 100.0f);
        g_qz_dbg[10] = (u32)(int)(g_dbg_qual[0] * 100.0f);
        g_qz_dbg[11] = (u32)(int)(g_dbg_qual[1] * 100.0f);
        g_qz_dbg[12] = (u32)(int)(dr.az_deg * 10.0f);
        Xil_DCacheFlushRange((UINTPTR)g_qz_dbg, sizeof(g_qz_dbg));

        /* ④ 合理性检查：物理上一条对角线的时延不可能超过 MAX_DELAY */
        if (fabsf(t02) > MAX_DELAY || fabsf(t13) > MAX_DELAY) {
            bad_frames++;
            telemetry_line(frame_no, 2, 0, 0, t02, t13, thr, bad_frames);
            xil_printf("#%04u  (skipped: TDOA out of range)\r\n", frame_no);
            frame_no++;
            continue;
        }

        /* ⑤ 方位角：最小二乘直接给出。
         *    四麦时 X=(t02-t13)/2、Y=(t02+t13)/2，
         *    θ = atan2(Y,X) = atan2(t02+t13, t02-t13)，与旧公式恒等。
         *    物理意义：声源相对 +X 轴（东）逆时针的角度
         *      东=0°  北=90°  西=180°  南=-90°
         *    方向严重反了？把其中一条对角线的两个端点宏对调即可。 */
        float az = dr.az_deg;
        az += AZ_OFFSET_DEG;
        if (az >  180.0f) az -= 360.0f;
        if (az < -180.0f) az += 360.0f;

        /* ⑥ 自检：判据必须与"阵列到底多大"无关，否则标定值一偏就全判可疑。
         *    三麦：最小二乘残差 + 孔径量级 —— θ=atan2(Y,X) 只认比值，
         *          间距标称错几倍角度照样对，只有三条边互相矛盾时才判可疑。
         *    四麦：没有冗余方程，残差恒 0，沿用原来的标称孔径判据。 */
        int suspect;
        if (dr.npair >= 3) {
            /* 只认残差。孔径 A 是"声源离中垂线多远"的度量，正对中垂线时
             * 本来就可以接近 0 —— 拿 A 去比标称边长，会把合法角度判成可疑，
             * 这正是此前页面永远"待确认"的原因之一。 */
            /* 残差看"六对时延是否互相自洽"，孔径看"方向是否真的可辨"。
             * 正对中垂线时孔径本来就接近 0 —— 此时 atan2(Y,X) 由噪声决定，
             * 再自洽也没有意义，必须判成不可用，否则箭头会满屏乱指。 */
            int ok_res = (dr.resid < 0.45f);
            int ok_ap  = (dr.aperture > 0.10f * TAU_SIDE);
            suspect = !(ok_res && ok_ap);
        } else {
            /* 只剩两条配对时没有冗余方程，只能看峰锐度和孔径 */
            suspect = (dr.qmin < 3.0f) || (dr.aperture < 0.10f * TAU_SIDE);
        }
        if (suspect) bad_frames++;

        /* ⑦ 3 点中值：只让"自检通过"的帧进历史，野值直接丢掉。
         *    一次拍手通常能出 2~5 帧，取中值比单帧稳得多，
         *    代价是最多滞后 2 帧（≈42 ms），对一次性拍手无感。 */
        float az_out = suspect ? az : az_smooth(az);

        /* ⑧ 打印：主行只留"方位 + 响度"，其余细节放到 DEBUG_DLY 里，
         *    平时看着清爽。xil_printf 不支持 %f，统一用 round10 拆整数。 */
        int az_10 = round10(az_out);
        int az_i = az_10 / 10, az_f = az_10 % 10;
        if (az_f < 0) az_f = -az_f;

        xil_printf("#%04u  AZ = %d.%d deg%c   lvl = %d.%d dB\r\n",
                   frame_no, az_i, az_f, suspect ? '?' : ' ',
                   l10/10, (l10<0?-l10:l10)%10);
        telemetry_line(frame_no, 1, az_10, suspect ? 0 : 1,
                       t02, t13, thr, bad_frames);

#if DEBUG_DLY
        {
            int d02 = (int)(t02 * 100.0f);
            int d13 = (int)(t13 * 100.0f);
            char s02 = (d02 < 0) ? '-' : '+';  int a02 = (d02 < 0) ? -d02 : d02;
            char s13 = (d13 < 0) ? '-' : '+';  int a13 = (d13 < 0) ? -d13 : d13;
            int t10 = round10(thr);
            int azr = round10(az);
            int rp = (int)(dr.resid * 100.0f);
            int ap = (int)(g_last_ap * 100.0f);
            xil_printf("        t02=%c%d.%02d t13=%c%d.%02d  rawAZ=%d.%d  "
                       "mask=0x%X  resid=%d%%  ap=%d%%  thr=%d.%d  bad=%u\r\n",
                       s02, a02/100, a02%100,
                       s13, a13/100, a13%100,
                       azr/10, (azr<0?-azr:azr)%10,
                       mask, rp, ap,
                       t10/10, (t10<0?-t10:t10)%10,
                       bad_frames);
        }
#endif

        frame_no++;
    }
    return 0;   /* 永远到不了这里 */
}
