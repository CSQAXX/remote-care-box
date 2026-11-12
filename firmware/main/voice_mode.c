/*
 * voice_mode.c —— 一套固件里的两种语音模式
 *
 * 见 voice_mode.h 的设计说明。实现刻意保持很薄:
 *   - 一个枚举 + 一个时间戳, 不建任务、不申请堆
 *   - 切换只打日志 + 弹一条屏幕提示
 *   - 对话统一从 voice_mode_start_turn_at() 出去, 方便以后换后端
 */

#include "voice_mode.h"

#include "ai_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "lcd_prompt.h"

static const char *TAG = "voice_mode";

static volatile voice_mode_t s_mode = VOICE_MODE_CARE;
static volatile int64_t s_last_active_ms = 0;

const char *voice_mode_name(voice_mode_t mode)
{
    return mode == VOICE_MODE_XIAOZHI ? "小智" : "看护对讲";
}

void voice_mode_set(voice_mode_t mode)
{
    if (mode != VOICE_MODE_CARE && mode != VOICE_MODE_XIAOZHI) {
        return;
    }

    voice_mode_t old = s_mode;
    s_mode = mode;
    s_last_active_ms = esp_timer_get_time() / 1000;

    if (old == mode) {
        /* 默认就是看护对讲, 平时喊「晴老师」不弹提示 —— 不干扰看护画面 */
        return;
    }

    ESP_LOGI(TAG, "模式切换: %s -> %s", voice_mode_name(old), voice_mode_name(mode));
    lcd_prompt_show(PROMPT_CONTENT,
                    mode == VOICE_MODE_XIAOZHI ? "小智模式" : "看护对讲模式",
                    3000);
}

voice_mode_t voice_mode_get(void)
{
    /*
     * 惰性超时回退: 不额外建任务, 每次查询顺手判一次。
     * 老人喊过「小康同学」却一直没再说话, 十分钟后自动回看护,
     * 免得盒子长期停在聊天模式把看护的语音入口占着。
     */
    if (s_mode == VOICE_MODE_XIAOZHI && s_last_active_ms > 0) {
        int64_t now_ms = esp_timer_get_time() / 1000;
        if (now_ms - s_last_active_ms > VOICE_MODE_AUTO_BACK_MS) {
            ESP_LOGI(TAG, "小智模式空闲超时, 自动回到看护对讲");
            voice_mode_set(VOICE_MODE_CARE);
        }
    }
    return s_mode;
}

esp_err_t voice_mode_start_turn_at(int64_t start_pos)
{
    s_last_active_ms = esp_timer_get_time() / 1000;
    voice_mode_t m = s_mode;

    /*
     * 第一阶段: 两种模式共用已验证的 /v1/turn 链路, 切过去立刻能聊天。
     *
     * TODO(第二阶段): m == VOICE_MODE_XIAOZHI 时改走小智云端对话协议,
     *                 m == VOICE_MODE_CARE 保持现在的 ai_client。
     *                 分流只在这一处, 唤醒词/屏幕/交互都不用改。
     */
    ESP_LOGI(TAG, "发起一轮对话 (模式=%s, 起句点=%lld)",
             voice_mode_name(m), (long long)start_pos);
    return ai_client_start_turn_at(start_pos);
}

void voice_mode_dump_status(void)
{
    voice_mode_t m = voice_mode_get();
    ESP_LOGI(TAG, "语音模式: %s (%d) | 口令: 「晴老师」=看护对讲, 「小康同学」=小智 | 串口 voice care / voice xiaozhi 可手动切",
             voice_mode_name(m), (int)m);
}
