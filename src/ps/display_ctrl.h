#ifndef DISPLAY_CTRL_H
#define DISPLAY_CTRL_H

#include "xil_io.h"
#include "xil_types.h"

/* axi_gpio_1 @ 0x41210000，双通道 32 位全输出 */
#define LCD_LED_GPIO_BASE 0x41210000UL
#define GPIO_CH1_DATA (LCD_LED_GPIO_BASE + 0x00u)   /* fill/state/db10 */
#define GPIO_CH2_DATA (LCD_LED_GPIO_BASE + 0x08u)   /* led_color/led_update */

/* 状态（与 lcd_ring_ui 的 i_state 一致） */
#define ST_IDLE  0
#define ST_QUIET 1
#define ST_WARN  2
#define ST_ALARM 3

/* 灯环 GRB 颜色（G 高 8 位 / R 中 8 位 / B 低 8 位，与色环状态同色） */
/* 16 颗 @ 5V 使用低亮度颜色；按每颗单色通道最大 20mA 保守估算，
 * 色值三通道代数和最大为 236/255，灯串峰值约 296mA；
 * 计入 16 颗静态电流后仍约 312mA，满足 0.5A 供电余量。 */
#define LED_IDLE  0x300060u   /* R=00 G=48 B=96  #003060 深蓝 */
#define LED_QUIET 0x6B0A3Cu   /* R=0A G=6B B=3C  #0A6B3C 暗绿 */
#define LED_WARN  0x5A8A08u   /* R=8A G=5A B=08  #8A5A08 暗琥 */
#define LED_ALARM 0x23A618u   /* R=A6 G=23 B=18  #A62318 暗红 */

/* 状态分档的亮度上限（百分比）。每个档位固定，不会随地噪声随机抖动：
 * IDLE 12% 常亮；QUIET 20% 常亮；WARN 25%..65% 呼吸；ALARM 40%..90% 呼吸。
 * 按 WS2812B 单通道 20mA 的保守模型，最高瞬时灯串电流约 282mA。 */
#define LED_IDLE_PCT        12u
#define LED_QUIET_PCT       20u
#define LED_WARN_MIN_PCT    25u
#define LED_WARN_MAX_PCT    65u
#define LED_ALARM_MIN_PCT   40u
#define LED_ALARM_MAX_PCT   90u

/* A 计权（队友 spl_meter 的 8 段表，单位 dB，按中心频率分段） */
static inline float a_weight(float hz)
{
    if (hz < 177.0f)  return -16.1f;
    if (hz < 354.0f)  return -8.6f;
    if (hz < 707.0f)  return -3.2f;
    if (hz < 1414.0f) return 0.0f;
    if (hz < 2828.0f) return 1.2f;
    if (hz < 5657.0f) return 1.0f;
    if (hz < 11314.0f) return -1.1f;
    return -6.6f;
}

static inline u32 led_color_for_state(int state)
{
    switch (state) {
        case ST_QUIET: return LED_QUIET;
        case ST_WARN:  return LED_WARN;
        case ST_ALARM: return LED_ALARM;
        default:       return LED_IDLE;
    }
}

/* 按百分比缩放 GRB（0..100）。保持色相不变，仅降低三个通道占空比。 */
static inline u32 led_scale_grb(u32 grb, u32 pct)
{
    u32 g = (((grb >> 16) & 0xFFu) * pct) / 100u;
    u32 r = (((grb >>  8) & 0xFFu) * pct) / 100u;
    u32 b = (( grb        & 0xFFu) * pct) / 100u;
    return (g << 16) | (r << 8) | b;
}

/* 色环屏幕和灯环分开写：屏幕约 1 Hz，灯环约 24 Hz 呼吸。 */
static inline void display_update_screen(int fill, int state, int db10)
{
    u32 c1 = ((u32)(db10 & 0xFFF) << 10) |
             ((u32)(state & 0x3) << 8) |
             (u32)(fill & 0xFF);
    Xil_Out32(GPIO_CH1_DATA, c1);
}

static inline void display_update_led(u32 led_color, int led_update)
{
    u32 c2 = ((led_update ? 1u : 0u) << 24) | (led_color & 0xFFFFFFu);
    Xil_Out32(GPIO_CH2_DATA, c2);
}

/* 保留合并写的原接口，便于旧版应用继续编译。 */
static inline void display_update(int fill, int state, int db10, u32 led_color, int led_update)
{
    display_update_screen(fill, state, db10);
    display_update_led(led_color, led_update);
}

#endif /* DISPLAY_CTRL_H */
