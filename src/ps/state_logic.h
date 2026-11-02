#ifndef QZ_STATE_LOGIC_H
#define QZ_STATE_LOGIC_H

/* QuietZone 状态值：与 display_ctrl.h / lcd_ring_ui.v 保持一致。 */
#define QZ_STATE_IDLE  0
#define QZ_STATE_QUIET 1
#define QZ_STATE_WARN  2
#define QZ_STATE_ALARM 3

/* 累计时长与容错窗口。 */
#define QZ_WARN_TO_ALARM_MS      4000u
#define QZ_ACTIVE_GAP_GRACE_MS    600u
#define QZ_WARN_RELEASE_MS      2000u
#define QZ_ALARM_RELEASE_MS     3000u

typedef struct {
    int      state;
    unsigned warn_ms;      /* WARN 状态实际保持时间 */
    unsigned active_ms;    /* 超过门限的活跃声音时间，仅诊断 */
    unsigned speech_ms;    /* 人声活跃时间，仅诊断 */
    unsigned other_ms;     /* 非人声活跃时间，仅诊断 */
    unsigned below_ms;     /* 连续低于门限时间 */
} qz_state_machine_t;

typedef struct {
    float flatness;       /* 几何均值 / 算术均值 */
    float centroid_hz;
    float flux;           /* 归一化谱通量 */
    float lowratio;       /* 500 Hz 以下能量占比 */
    float peak_ratio;     /* 最强单谱线能量占比 */
    float zcr_pct;
} qz_speech_features_t;

static inline void qz_state_reset(qz_state_machine_t *m)
{
    m->state = QZ_STATE_QUIET;
    m->warn_ms = 0u;
    m->active_ms = 0u;
    m->speech_ms = 0u;
    m->other_ms = 0u;
    m->below_ms = 0u;
}

/*
 * 每帧调用一次。active=本帧超过自适应声音门限，speech 仅保留为分类诊断。
 * 任一首个活跃帧进入 WARN；WARN 实际保持 4 s 后统一进入 ALARM。
 * 4 s 计时在短暂回落时继续，只有连续安静 2 s 才退出 WARN；ALARM 需连续安静 3 s 才回 QUIET。
 */
static inline int qz_state_update(qz_state_machine_t *m, int active, int speech,
                                  unsigned dt_ms)
{
    if (!active) {
        m->below_ms += dt_ms;

        /* 短回落只停止活跃帧统计，不清掉 WARN 的实际保持时间。 */
        if (m->below_ms >= QZ_ACTIVE_GAP_GRACE_MS) {
            m->speech_ms = 0u;
            m->other_ms = 0u;
        }

        if (m->state == QZ_STATE_ALARM) {
            if (m->below_ms >= QZ_ALARM_RELEASE_MS) {
                qz_state_reset(m);
            }
        } else if (m->state == QZ_STATE_WARN) {
            if (m->below_ms >= QZ_WARN_RELEASE_MS) {
                qz_state_reset(m);
            } else {
                m->warn_ms += dt_ms;
                if (m->warn_ms >= QZ_WARN_TO_ALARM_MS) {
                    m->state = QZ_STATE_ALARM;
                }
            }
        }
        return m->state;
    }

    m->below_ms = 0u;

    if (m->state == QZ_STATE_ALARM) {
        return m->state;
    }

    if (m->state != QZ_STATE_WARN) {
        m->state = QZ_STATE_WARN;
        m->warn_ms = 0u;
        m->active_ms = 0u;
        m->speech_ms = 0u;
        m->other_ms = 0u;
    }

    m->warn_ms += dt_ms;
    m->active_ms += dt_ms;
    if (speech) {
        m->speech_ms += dt_ms;
        m->other_ms = (m->other_ms > dt_ms) ? (m->other_ms - dt_ms) : 0u;
    } else {
        m->other_ms += dt_ms;
        m->speech_ms = (m->speech_ms > dt_ms) ? (m->speech_ms - dt_ms) : 0u;
    }

    if (m->warn_ms >= QZ_WARN_TO_ALARM_MS) {
        m->state = QZ_STATE_ALARM;
    }
    return m->state;
}

/*
 * 与硬件 classifier.v 同口径的简化打分卡。谱平坦度、质心、谱通量、低频占比
 * 分别计分；纯音和宽频噪声另外扣分，避免把稳态声音误判成人声。
 * 返回 0..100，>=60 判为人声。
 */
static inline int qz_speech_confidence(const qz_speech_features_t *f)
{
    int score = 0;
    float flat_hi = 0.70f * 1.40f;

    if (f->flatness < 0.70f)       score += 2;
    else if (f->flatness > flat_hi) score -= 2;

    /* 小尺寸麦克风与远场语音的谱质心常落在 5–9 kHz；上限取 10 kHz，
     * 同时仍将 300 Hz 以下隆隆声和接近奈奎斯特的白噪声排除。 */
    if (f->centroid_hz >= 250.0f && f->centroid_hz <= 10000.0f)
        score += 2;
    else
        score -= 1;

    if (f->flux > 0.1465f)         score += 2;
    if (f->lowratio > 0.586f)      score -= 2;

    /* 单音/啸叫的谱峰过于集中；人声是谐波群，最强线通常不会压满全谱。 */
    if (f->peak_ratio > 0.72f)     score -= 4;
    if (f->zcr_pct < 0.8f || f->zcr_pct > 35.0f) score -= 2;

    int conf = 50 + 10 * score;
    if (conf < 0) conf = 0;
    if (conf > 100) conf = 100;
    return conf;
}

#endif /* QZ_STATE_LOGIC_H */
