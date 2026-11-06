/*
 * 屏幕提示 / 求助反馈层 —— 需求文档 §3.8
 *
 * ============================ 需求原文 ============================
 * §3.8 求助与屏幕提示
 *
 * | 事件 | 触发 | 行为 |
 * | 求助 | 物理键短按 | 立即上报 help_pressed, 屏幕显示「已通知家人」,
 * |     10s 内可长按撤销 |
 * | 取消 | 长按 | 上报 help_canceled, 后台关闭该告警 |
 * | 家人正在看画面 | 收到 0x11 指令 | 屏幕显示「家人正在看画面」,
 * |     5s 后自动熄灭 |
 * | 家人正在呼叫 | 收到 0x11 指令 | 屏幕显示「家人正在呼叫您」, 伴随提示音 |
 * | 干预内容播放 | 收到 0x11 指令 | 屏幕显示「该活动一下」等文案 + 播放音频 |
 *
 * 屏幕提示是【防惊吓机制】: 老人在不知情下被看到/被听到容易恐慌,
 * 所有远程行为都要在设备端有可见提示。
 * ==================================================================
 *
 * ============================ 实现说明 ============================
 * 本模块负责:
 *   1. 【设备端】按键短按 -> 屏幕显示「已通知家人」+ 上报事件
 *   2. 【设备端】长按 -> 撤销求助 -> 屏幕显示「求助已取消」
 *   3. 【下行】收到家人指令 -> 屏幕显示对应提示 (看画面/呼叫/内容干预)
 *
 * 关键设计:
 *   - 提示是【叠加层】, 不是替换摄像头画面。
 *     老人需要看到家人还在看着自己, 同时知道"有人在看"。
 *     所以提示画在画面【下半部分】的一个半透明感黑条上。
 *   - 提示有【自动超时】, 到期自动消失, 不会永久挡住画面。
 *   - 提示由一个独立任务管理, 不阻塞按键任务和画面任务。
 *
 * 【为什么不用 LVGL】
 *   只需要"在画面上叠一条带字的横幅", LVGL 太重 (内部 RAM 紧张,
 *   之前音频排查时内部堆只剩 28KB)。自己画只要一个行缓冲 (640 字节)。
 * ==================================================================
 */

#ifndef LCD_PROMPT_H
#define LCD_PROMPT_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- 提示类型 ---- */
typedef enum {
    PROMPT_NONE = 0,
    PROMPT_HELP_SENT,      /* 「已通知家人」 */
    PROMPT_HELP_CANCELED,  /* 「求助已取消」 */
    PROMPT_WATCHING,       /* 「家人正在看画面」 */
    PROMPT_CALLING,        /* 「家人正在呼叫您」 */
    PROMPT_CONTENT,        /* 「该活动一下」等干预文案 */
} prompt_type_t;

/* ---- 提示持续时间 (ms) ---- */
#define PROMPT_DUR_HELP_SENT     10000   /* §3.8: 求助提示 10s 内可长按撤销 */
#define PROMPT_DUR_HELP_CANCELED  5000
#define PROMPT_DUR_WATCHING       5000   /* §3.8: 家人正在看画面, 5s 后熄灭 */
#define PROMPT_DUR_CALLING        8000
#define PROMPT_DUR_CONTENT        10000

/**
 * @brief 启动屏幕提示任务
 */
esp_err_t lcd_prompt_start(void);

/**
 * @brief 在屏幕上显示一条提示 (自动到期消失)
 *
 * @param type    提示类型 (决定默认文案和时长)
 * @param text    自定义文案 (NULL = 用该类型的默认文案)
 * @param duration_ms  显示时长 (0 = 用该类型的默认时长)
 */
void lcd_prompt_show(prompt_type_t type, const char *text, uint32_t duration_ms);

/**
 * @brief 立刻撤掉当前提示
 */
void lcd_prompt_hide(void);

/**
 * @brief 当前是否有提示在显示
 */
bool lcd_prompt_is_active(void);

/**
 * @brief 当前提示的类型 (PROMPT_NONE = 没有)
 */
prompt_type_t lcd_prompt_current_type(void);

#ifdef __cplusplus
}
#endif

#endif /* LCD_PROMPT_H */
