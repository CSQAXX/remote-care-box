/*
 * 屏幕提示 / 求助反馈层 —— 实现
 *
 * 见 lcd_prompt.h 的说明 (需求文档 §3.8)。
 *
 * ============================ 核心机制 ============================
 * 提示不是【替换】画面, 而是【叠加】在画面上的一条横幅。
 *
 * 为什么要叠加而不是替换:
 *   §3.8 说屏幕提示是"防惊吓机制" —— 让老人知道有人在看/在呼叫。
 *   但如果提示把摄像头画面整个盖住, 老人反而会紧张
 *   ("我的画面去哪了?")。所以只在画面【下半部分】叠一条横幅,
 *   上半部分仍然能看到家人, 既有知情感又不慌。
 *
 * 横幅的画法:
 *   1. 先用黑色矩形覆盖横幅区域 (遮住下方的画面)
 *   2. 再在上面画文字
 *
 * 刷新的时机:
 *   - 提示出现时画一次
 *   - 提示到期时【擦掉】(画回摄像头的这一块)
 *
 * 擦掉的问题: 摄像头画面一直在变, 我们不知道原来那一块是什么。
 *   解法: 提示显示期间, 【暂停摄像头直绘】(让出屏幕),
 *   提示到期后恢复直绘 —— 下一帧画面自然会覆盖回来。
 *   这样提示期间的画面是"静止的旧画面 + 横幅", 不会撕裂。
 * ==================================================================
 */

#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"

#include "lcd_st7789.h"
#include "lcd_text.h"
#include "lcd_prompt.h"
#include "audio_board.h"

static const char *TAG = "prompt";

/* 横幅位置: 画面下半部分, 高度 40 像素 (够放两行 16px 字) */
#define PROMPT_BANNER_H    40
#define PROMPT_BANNER_Y    (240 - PROMPT_BANNER_H)

/* 横幅配色 */
#define PROMPT_BG_COLOR    0x0000   /* 黑 */
#define PROMPT_FG_COLOR    0xFFE0   /* 黄 —— 醒目, 老人一眼能看见 */
#define PROMPT_FG_ALERT    0xF800   /* 红 —— 求助相关 */

/* 任务参数 */
#define PROMPT_TASK_STACK  4096
#define PROMPT_TASK_PRIO   4

/* ---------------- 共享状态 ---------------- */

typedef struct {
    volatile bool      active;
    volatile prompt_type_t type;
    char               text[128];    /* 两行字幕最多 34 个汉字 (UTF-8 3 字节/字) */
    uint32_t           duration_ms;
    int64_t            show_ms;      /* 显示起始时间 */
    int64_t            hide_ms;      /* 到期擦除时间 */
    uint16_t           fg;           /* 本条提示的文字色 */
    uint16_t           bg;           /* 本条提示的背景色 */
    bool               dirty;        /* 需要重画 */
    bool               need_erase;   /* 需要擦除 */
    SemaphoreHandle_t  mutex;
} prompt_state_t;

static prompt_state_t s_prompt = { 0 };
static TaskHandle_t   s_prompt_task = NULL;

/* ---------------- 默认文案 ---------------- */

static const char *prompt_default_text(prompt_type_t type)
{
    switch (type) {
    case PROMPT_HELP_SENT:     return "已通知家人";
    case PROMPT_HELP_CANCELED: return "求助已取消";
    case PROMPT_WATCHING:      return "家人正在看画面";
    case PROMPT_CALLING:       return "家人正在呼叫您";
    case PROMPT_CONTENT:       return "该活动一下";
    default:                   return "";
    }
}

static uint32_t prompt_default_duration(prompt_type_t type)
{
    switch (type) {
    case PROMPT_HELP_SENT:     return PROMPT_DUR_HELP_SENT;
    case PROMPT_HELP_CANCELED: return PROMPT_DUR_HELP_CANCELED;
    case PROMPT_WATCHING:      return PROMPT_DUR_WATCHING;
    case PROMPT_CALLING:       return PROMPT_DUR_CALLING;
    case PROMPT_CONTENT:       return PROMPT_DUR_CONTENT;
    default:                   return 5000;
    }
}

/* ---------------- 对外接口 ---------------- */

void lcd_prompt_show(prompt_type_t type, const char *text, uint32_t duration_ms)
{
    /*
     * 老人看护的默认外观: 黑底黄字 (求助类红字) —— 和以前完全一致。
     * 颜色在这里就定好再往下传, 这样下面 prompt_task 只管照用,
     * 不会把调用方明确指定的黑色 (0x0000) 误判成"没指定"。
     */
    uint16_t fg = PROMPT_FG_COLOR;
    if (type == PROMPT_HELP_SENT || type == PROMPT_HELP_CANCELED) {
        fg = PROMPT_FG_ALERT;
    }
    lcd_prompt_show_styled(type, text, duration_ms, fg, PROMPT_BG_COLOR);
}

void lcd_prompt_show_styled(prompt_type_t type, const char *text,
                            uint32_t duration_ms, uint16_t fg, uint16_t bg)
{
    if (type == PROMPT_NONE) {
        return;
    }

    const char *msg = (text && text[0]) ? text : prompt_default_text(type);
    if (duration_ms == 0) {
        duration_ms = prompt_default_duration(type);
    }

    if (s_prompt.mutex) {
        xSemaphoreTake(s_prompt.mutex, portMAX_DELAY);
    }

    /*
     * 关键: 换提示类型时, 先把旧的擦掉, 否则新旧文字会叠在一起。
     */
    if (s_prompt.active && s_prompt.type != type) {
        s_prompt.need_erase = true;
    }

    s_prompt.type     = type;
    snprintf(s_prompt.text, sizeof(s_prompt.text), "%s", msg);
    s_prompt.duration_ms = duration_ms;
    s_prompt.fg       = fg;
    s_prompt.bg       = bg;
    s_prompt.show_ms  = esp_timer_get_time() / 1000;
    s_prompt.hide_ms  = s_prompt.show_ms + duration_ms;
    s_prompt.active   = true;
    s_prompt.dirty    = true;

    if (s_prompt.mutex) {
        xSemaphoreGive(s_prompt.mutex);
    }

    ESP_LOGI(TAG, "显示提示: %s (%u ms)", msg, (unsigned)duration_ms);
}

void lcd_prompt_hide(void)
{
    if (s_prompt.mutex) {
        xSemaphoreTake(s_prompt.mutex, portMAX_DELAY);
    }
    if (s_prompt.active) {
        s_prompt.need_erase = true;
        s_prompt.active = false;
    }
    if (s_prompt.mutex) {
        xSemaphoreGive(s_prompt.mutex);
    }
}

bool lcd_prompt_is_active(void)
{
    return s_prompt.active;
}

prompt_type_t lcd_prompt_current_type(void)
{
    return s_prompt.type;
}

/* ---------------- 内部绘制 ---------------- */

/*
 * 画一条横幅 (黑底 + 黄字)。
 *
 * 注意: 这里【不】去动摄像头画面 —— 只是在自己的区域画。
 * 提示期间摄像头直绘会被暂停 (见 main.c 的 lcd_draw_owner 机制),
 * 所以横幅不会和画面打架。
 */
static void draw_banner(const char *text, uint16_t fg, uint16_t bg)
{
    /* 1) 铺背景 (老人是黑, 小电脑是白) */
    lcd_fill_rect(0, PROMPT_BANNER_Y, 320, PROMPT_BANNER_H, bg);

    /*
     * 2) 非黑底时, 顶端加一条浅灰分隔线。
     *    白底横幅和上方画面之间没有分界会糊成一片, 加 1 像素就够了。
     *    黑底不加 —— 保持老人那边原来的样子。
     */
    if (bg != PROMPT_BG_COLOR) {
        lcd_fill_rect(0, PROMPT_BANNER_Y, 320, 1, 0xC618);
    }

    /*
     * 3) 画文字。
     *
     * 文案里允许有一个 '\n' 表示换两行: 横幅 40 像素正好放两行 16px 字,
     * 小电脑的长回话靠这个才不会被截断。
     * 没有换行符就照旧单行居中 —— 老人看护那些文案都不带换行, 行为不变。
     */
    const char *nl = strchr(text, '\n');
    if (nl == NULL) {
        lcd_draw_text_centered(0, PROMPT_BANNER_Y, 320, PROMPT_BANNER_H,
                               text, fg, bg, 1);
        return;
    }

    /* 两行: 各占一半高度 (20px), 字高 16px, 每行上下各留 2px */
    int half = PROMPT_BANNER_H / 2;
    char first[64];
    int n = (int)(nl - text);
    if (n > (int)sizeof(first) - 1) {
        n = (int)sizeof(first) - 1;
    }
    memcpy(first, text, (size_t)n);
    first[n] = '\0';

    lcd_draw_text_centered(0, PROMPT_BANNER_Y, 320, half, first, fg, bg, 1);
    lcd_draw_text_centered(0, PROMPT_BANNER_Y + half, 320, half, nl + 1, fg, bg, 1);
}

/* ---------------- 提示任务 ---------------- */

static void prompt_task(void *arg)
{
    bool banner_shown = false;

    while (true) {
        bool do_draw = false;
        bool do_erase = false;
        prompt_type_t type = PROMPT_NONE;
        uint16_t draw_fg = 0;
        uint16_t draw_bg = PROMPT_BG_COLOR;
        char text[128];

        if (s_prompt.mutex) {
            xSemaphoreTake(s_prompt.mutex, portMAX_DELAY);
        }

        int64_t now = esp_timer_get_time() / 1000;

        if (s_prompt.need_erase) {
            s_prompt.need_erase = false;
            do_erase = true;
        }

        if (s_prompt.dirty) {
            s_prompt.dirty = false;
            do_draw = true;
            type = s_prompt.type;
            draw_fg = s_prompt.fg;
            draw_bg = s_prompt.bg;
            snprintf(text, sizeof(text), "%s", s_prompt.text);
        }

        /* 到期 */
        if (s_prompt.active && now >= s_prompt.hide_ms) {
            s_prompt.active = false;
            s_prompt.need_erase = true;
            do_erase = true;
            ESP_LOGI(TAG, "提示到期, 擦除");
        }

        if (s_prompt.mutex) {
            xSemaphoreGive(s_prompt.mutex);
        }

        if (do_erase) {
            /*
             * 擦除: 不需要我们画什么 —— 提示结束时恢复摄像头直绘,
             * 下一帧画面自然就覆盖回来了。这里只是把标志清掉。
             *
             * 但如果这时摄像头还没接管 (比如画面任务还没跑),
             * 屏幕会留一条黑条。所以这里主动请求重画:
             * main.c 里的画面恢复逻辑会重画整屏。
             */
            banner_shown = false;
            if (do_draw) {
                /* 擦除的同时又有新提示 —— 顺序是先擦后画 */
            }
            /* 通知画面层: 可以恢复直绘了 */
            extern void lcd_view_resume(void);
            lcd_view_resume();
        }

        if (do_draw) {
            /*
             * 颜色在 lcd_prompt_show() / lcd_prompt_show_styled() 里就已经定好,
             * 这里必须【照用】, 不能再"判断 0 就换默认色" ——
             * 黑字就是 0x0000, 那样会被误判成没指定, 白底上变成黄字看不见。
             */
            uint16_t fg = draw_fg;

            /*
             * 先通知画面层暂停直绘, 再画横幅。
             * 顺序很重要: 不暂停的话, 摄像头的下一帧会把横幅盖掉。
             */
            extern void lcd_view_pause(void);
            lcd_view_pause();

            /* 打出来才能确认配色真的传对了 (白底黑字 = bg 0xFFFF / fg 0x0000) */
            ESP_LOGI(TAG, "画横幅 fg=0x%04X bg=0x%04X", fg, draw_bg);

            draw_banner(text, fg, draw_bg);
            banner_shown = true;
        }

        (void)banner_shown;
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

esp_err_t lcd_prompt_start(void)
{
    if (s_prompt_task != NULL) {
        return ESP_OK;
    }

    s_prompt.mutex = xSemaphoreCreateMutex();
    if (s_prompt.mutex == NULL) {
        ESP_LOGE(TAG, "创建互斥量失败");
        return ESP_ERR_NO_MEM;
    }

    BaseType_t ok = xTaskCreatePinnedToCore(prompt_task, "lcd_prompt",
                                             PROMPT_TASK_STACK, NULL,
                                             PROMPT_TASK_PRIO, &s_prompt_task,
                                             APP_CORE_ID);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "创建提示任务失败");
        s_prompt_task = NULL;
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "屏幕提示任务已启动");
    return ESP_OK;
}
