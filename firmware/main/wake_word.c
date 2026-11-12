/*
 * wake_word.c —— 离线唤醒词「晴老师」(MultiNet7 / mn7_cn)
 *
 * 见 wake_word.h 的详细设计说明。
 *
 * 实现要点:
 *   1. esp_srmodel_init("model") 把 model 分区里的模型 mmap 进内存
 *   2. esp_mn_handle_from_name("mn7_cn") 拿到 MultiNet7 中文接口
 *   3. mn->create("mn7_cn", timeout) 建实例 (内部会读 vocab + commands_cn.txt)
 *   4. esp_mn_commands_clear() 清掉自带的 300 多条通用命令
 *      esp_mn_commands_add(0, "qing lao shi") 只留「晴老师」
 *      esp_mn_commands_update() 重新编译 FST
 *   5. 后台任务: 从共享读通道取 16k 单声道帧 -> mn->detect()
 *      命中 -> 记录绝对时间点 -> 通知 ai_client 开工
 *
 * 音频接续 (C2):
 *   命中时把【本模块的读游标位置】换算成绝对样本计数交给 ai_client,
 *   ai_client 用同一个绝对位置开始读 —— 这样唤醒词本身和它后面的
 *   半句话都不会丢, 而且两边不会重复也不会漏。
 */

#include "wake_word.h"

#ifdef ENABLE_PC_VOICE
#include "pc_voice.h"   /* PC_VOICE_PATCH v1 */
#endif

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "ai_client.h"
#include "audio_board.h"
#include "voice_mode.h"
#include "esp_log.h"
#include "esp_mn_iface.h"
#include "esp_mn_models.h"
#include "esp_mn_speech_commands.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "model_path.h"

static const char *TAG = "wake_word";

/* ---------------- 内部状态 ---------------- */

static TaskHandle_t s_task = NULL;
static volatile bool s_running = false;
static volatile bool s_ready = false;
static volatile uint32_t s_hits = 0;
static volatile float s_last_prob = 0.0f;
static volatile float s_threshold = WAKE_WORD_THRESHOLD;

static const esp_mn_iface_t *s_mn = NULL;
static model_iface_data_t *s_mn_data = NULL;
static srmodel_list_t *s_models = NULL;
static int s_reader = -1;         /* 共享读通道句柄 */

/* 命中时的绝对样本位置 (交给 ai_client 当起句点) */
static volatile int64_t s_hit_pos = -1;

/* ---------------- 工具 ---------------- */

/**
 * @brief 初始化 MultiNet7 中文模型并注册「晴老师」
 */
static esp_err_t wake_word_model_init(void)
{
    /* model 分区里的模型 (mn7_cn + vocab + fst/commands_cn.txt) */
    s_models = esp_srmodel_init("model");
    if (s_models == NULL) {
        ESP_LOGE(TAG, "model 分区加载失败 (烧录时有没有刷 srmodels.bin?)");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "model 分区已加载, 共 %d 个模型", s_models->num);

    /*
     * 从模型列表里找中文 MultiNet。
     * esp_srmodel_filter 的关键字是 "mn" (ESP_MN_PREFIX) + "cn" (ESP_MN_CHINESE),
     * 命中的是模型目录名, 比如 "mn7_cn"。
     */
    char *mn_name = esp_srmodel_filter(s_models, ESP_MN_PREFIX, ESP_MN_CHINESE);
    if (mn_name == NULL) {
        ESP_LOGE(TAG, "model 分区里没有中文 MultiNet 模型");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "使用模型: %s", mn_name);

    s_mn = esp_mn_handle_from_name(mn_name);
    if (s_mn == NULL) {
        ESP_LOGE(TAG, "拿不到 %s 的接口 (esp_mn_handle_from_name 返回 NULL)", mn_name);
        return ESP_FAIL;
    }

    s_mn_data = s_mn->create(mn_name, WAKE_WORD_TIMEOUT_MS);
    if (s_mn_data == NULL) {
        ESP_LOGE(TAG, "MultiNet7 实例创建失败");
        return ESP_FAIL;
    }

    /*
     * 【不要在这里调 esp_mn_commands_clear() / _add() / _update()】
     *
     * 实测 (2026-11-08, esp-sr 2.6.1): 这三行会让板子无限重启。
     * esp_mn_commands_update() 内部走 libfst.a 的 "Build fst from commands.",
     * 只编译【一条】命令时 FST 编译器会越界写, 触发
     *   Guru Meditation Error: Core 0 panic'ed (StoreProhibited)
     *   EXCVADDR: 0xfffffffc
     * 这跟 commands_cn.txt 被砍成 1 行时 create() 里 build_fsts 崩掉
     * 是同一个底层编译器的同一个 bug —— 命令表太短就会崩。
     *
     * 所以正确做法: 把「晴老师」直接写进 model 分区里的
     * commands_cn.txt (第 314 条, 官方 313 条原样保留),
     * 由 create() 自动注册。这样命令表有 314 条, FST 编译器正常工作,
     * 开机即用, 也不需要任何运行时注册。
     *
     * 「晴老师」的 command_id 就是它在 commands_cn.txt 里的行号 (314)。
     */
    ESP_LOGI(TAG, "命令词由 commands_cn.txt 自动注册 (第 %d 条)", WAKE_WORD_CMD_ID);

    /* 检测门限: 模型自带 0.90 对三字唤醒词偏严, 老人发音没那么标准 */
    s_mn->set_det_threshold(s_mn_data, s_threshold);

    int chunk = s_mn->get_samp_chunksize(s_mn_data);
    int rate = s_mn->get_samp_rate(s_mn_data);
    ESP_LOGI(TAG, "唤醒词「%s」就绪 (拼音 %s, 门限 %.2f, 帧长 %d @%dHz)",
             WAKE_WORD_NAME, WAKE_WORD_PINYIN, s_threshold, chunk, rate);

    s_mn->print_active_speech_commands(s_mn_data);

    return ESP_OK;
}

/* ---------------- 后台任务 ---------------- */

static void wake_word_task(void *arg)
{
    (void)arg;

    int chunk = s_mn->get_samp_chunksize(s_mn_data);
    if (chunk <= 0) {
        chunk = 512;
    }
    int16_t *buf = heap_caps_malloc(chunk * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buf == NULL) {
        ESP_LOGE(TAG, "缓冲分配失败");
        s_ready = false;
        vTaskDelete(NULL);
        return;
    }

    /* 最近一次读到的绝对位置 (命中时换算成起句点用) */
    int64_t cur_pos = audio_board_mono_total();
    uint32_t timeout_logged = 0;
    uint32_t nodata_run = 0;   /* 连续读不到上行音频的次数 */
    uint32_t dbg_tick  = 0;    /* 诊断打印节拍 */

    while (s_running) {
        int n = audio_board_read_mono_shared(s_reader, buf, chunk);
        if (n <= 0) {
            /*
             * 自愈: 连续约 2 秒读不到上行音频, 极可能是 s_downlink_active
             * 被某条异常路径卡在 true, 采集侧于是不再写上行缓冲。
             * 强制清零一次, 让识别引擎重新拿到数据流。
             */
            if (++nodata_run == 100) {
                ESP_LOGW(TAG, "诊断: 连续读不到上行音频, downlink=%d, 强制清零",
                         (int)audio_board_is_downlink_active());
                audio_board_set_downlink_active(false);
            }
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        nodata_run = 0;
        /*
         * 用读游标的真实位置, 而不是自己累加 ——
         * 读游标落后太多时 audio_board 会把它拉到最新一圈 (丢最旧的),
         * 这时候自己累加的 cur_pos 就对不上了。
         */
        int64_t now_pos = audio_board_reader_pos(s_reader);
        if (now_pos > 0) {
            cur_pos = now_pos;
        } else {
            cur_pos += n;
        }

        /*
         * 下行播放期间不喂识别器 —— 喇叭里放的是大模型回复,
         * 喂进去会把自己唤醒 (自问自答)。
         * 实际上 audio_board 在 downlink_active 时本来就不往环形缓冲写,
         * 这里再挡一道是防止状态切换的边界帧漏进来。
         */
        if (audio_board_is_downlink_active()) {
            continue;
        }

        /* 诊断: 每 100 帧(约 3.2 秒)报一次实际喂进识别引擎的样本数 */
        if ((++dbg_tick % 100) == 0) {
            ESP_LOGI(TAG, "诊断: 喂识别 n=%d pos=%lld", n, (long long)cur_pos);
        }

        esp_mn_state_t st = s_mn->detect(s_mn_data, buf);

        if (st == ESP_MN_STATE_DETECTED) {
            esp_mn_results_t *res = s_mn->get_results(s_mn_data);
            if (res != NULL && res->num > 0) {
                for (int i = 0; i < res->num; i++) {
                    int id = res->command_id[i];
                    if (id != WAKE_WORD_CMD_ID && id != WAKE_WORD_XIAOZHI_CMD_ID
#ifdef ENABLE_PC_VOICE      /* PC_VOICE_PATCH v1 */
                        && id != WAKE_WORD_PC_CMD_ID
#endif
                    ) {
                        continue;
                    }

                    /*
                     * 两句口令同时是「模式开关」:
                     *   「小康同学」-> 小智聊天; 「晴老师」-> 看护对讲。
                     * 看护那条链路一行没改, 只是中间多了一层模式分发。
                     * 见 voice_mode.h。
                     */
#ifdef ENABLE_PC_VOICE
                    /* PC_VOICE_PATCH v1: 「小电脑」单独一条路, 不进 voice_mode, 不改看护模式 */
                    if (id == WAKE_WORD_PC_CMD_ID) {
                        s_last_prob = res->prob[i];
                        s_hits++;
                        ESP_LOGI(TAG, "命中「小电脑」#%" PRIu32 " (概率 %.3f) -> 交给我的电脑",
                                 s_hits, s_last_prob);
                        int64_t pc_start = cur_pos - (WAKE_WORD_PREROLL_MS * 16);
                        s_hit_pos = pc_start;
                        esp_err_t pr = pc_voice_start_turn_at(pc_start);
                        if (pr != ESP_OK) {
                            ESP_LOGW(TAG, "交给电脑失败: %s", esp_err_to_name(pr));
                        }
                        break;
                    }
#endif
                    bool to_xiaozhi = (id == WAKE_WORD_XIAOZHI_CMD_ID);
                    const char *hit_name = to_xiaozhi ? WAKE_WORD_XIAOZHI_NAME : WAKE_WORD_NAME;

                    s_last_prob = res->prob[i];
                    s_hits++;
                    ESP_LOGI(TAG, "命中「%s」#%" PRIu32 " (概率 %.3f) @%" PRId64 " -> %s模式",
                             hit_name, s_hits, s_last_prob, cur_pos,
                             to_xiaozhi ? "小智" : "看护对讲");

                    /*
                     * 把起句点交给对话链路:
                     * 回退 WAKE_WORD_PREROLL_MS, 让这一句从头开始 ——
                     * 唤醒词本身 + 后面接的那半句, 都不会丢。
                     */
                    int64_t start = cur_pos - (WAKE_WORD_PREROLL_MS * 16);
                    s_hit_pos = start;

                    voice_mode_set(to_xiaozhi ? VOICE_MODE_XIAOZHI : VOICE_MODE_CARE);
                    esp_err_t r = voice_mode_start_turn_at(start);
                    if (r != ESP_OK) {
                        ESP_LOGW(TAG, "发起对话失败: %s", esp_err_to_name(r));
                    }
                    break;
                }
            }
            /* 复位模型, 准备下一轮 */
            s_mn->clean(s_mn_data);
        } else if (st == ESP_MN_STATE_TIMEOUT) {
            /* 静默太久, 模型自己超时了 —— 复位继续听 */
            s_mn->clean(s_mn_data);
            timeout_logged++;
            if (timeout_logged % 30 == 1) {
                ESP_LOGD(TAG, "识别超时, 复位继续 (第 %" PRIu32 " 次)", timeout_logged);
            }
        }
    }

    free(buf);
    s_task = NULL;
    vTaskDelete(NULL);
}

/* ---------------- 对外接口 ---------------- */

esp_err_t wake_word_start(void)
{
    if (s_running) {
        return ESP_OK;
    }

    if (wake_word_model_init() != ESP_OK) {
        wake_word_stop();
        return ESP_FAIL;
    }

    /* 申请独立的麦克风读游标 (不和子女端听声音那条链路抢) */
    s_reader = audio_board_reader_open();
    if (s_reader < 0) {
        ESP_LOGE(TAG, "申请共享读通道失败");
        return ESP_FAIL;
    }

    s_running = true;
    s_ready = true;

#ifdef ENABLE_PC_VOICE
    /* 「小电脑」工作线程在开机时建好 —— 唤醒时再建会因内部内存碎片失败 */
    pc_voice_prepare();
#endif

    /*
     * 钉到 CPU1、优先级提到 6:
     * 看护盒子几乎所有任务 (摄像头 cam_producer、音频采集、推送、看门狗)
     * 都挤在 CPU0 (APP_CORE_ID=0)，而摄像头 cam_producer 优先级 6 长期占满 CPU0，
     * 原本没钉核、优先级 5 的 wake_word 会和它抢同一核还低一头，被持续饿死、
     * 连看门狗都喂不上 -> 喊话听不见。
     * 挪到 CPU1 后物理隔离，MultiNet7 推理独占一个核，麦克风共享环形缓冲是
     * 单写(core0 audio_cap)单读(core1 wake_word)无锁结构，跨核读安全。
     */
    xTaskCreatePinnedToCore(wake_word_task, "wake_word", 6144, NULL, 6, &s_task, 1);
    if (s_task == NULL) {
        s_running = false;
        s_ready = false;
        audio_board_reader_close(s_reader);
        s_reader = -1;
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "唤醒词「%s」监听已启动 (串口命令 wake 查看状态)", WAKE_WORD_NAME);
    return ESP_OK;
}

void wake_word_stop(void)
{
    s_running = false;
    s_ready = false;
    if (s_reader >= 0) {
        audio_board_reader_close(s_reader);
        s_reader = -1;
    }
    if (s_mn_data && s_mn) {
        s_mn->destroy(s_mn_data);
        s_mn_data = NULL;
    }
    if (s_models) {
        esp_srmodel_deinit(s_models);
        s_models = NULL;
    }
}

bool wake_word_is_ready(void)
{
    return s_ready;
}

uint32_t wake_word_hit_count(void)
{
    return s_hits;
}

float wake_word_last_prob(void)
{
    return s_last_prob;
}

esp_err_t wake_word_set_threshold(float threshold)
{
    if (threshold < 0.0f || threshold > 0.9999f) {
        return ESP_ERR_INVALID_ARG;
    }
    s_threshold = threshold;
    if (s_mn && s_mn_data) {
        s_mn->set_det_threshold(s_mn_data, threshold);
    }
    ESP_LOGI(TAG, "检测门限 -> %.2f", threshold);
    return ESP_OK;
}

esp_err_t wake_word_trigger_manual(void)
{
    ESP_LOGI(TAG, "手动唤醒「%s」", WAKE_WORD_NAME);
    s_hits++;
    /* 手动触发没有"命中位置", 从现在开始录 */
    return ai_client_start_turn_at(-1);
}

void wake_word_dump_status(void)
{
    ESP_LOGI(TAG, "--- 唤醒词状态 ---");
    ESP_LOGI(TAG, "  名称: %s", WAKE_WORD_NAME);
    ESP_LOGI(TAG, "  拼音: %s (无声调, 必须和 mn7 词表一致)", WAKE_WORD_PINYIN);
    ESP_LOGI(TAG, "  命令ID: %d", WAKE_WORD_CMD_ID);
    ESP_LOGI(TAG, "  就绪: %s", s_ready ? "是" : "否");
    ESP_LOGI(TAG, "  命中次数: %" PRIu32, s_hits);
    ESP_LOGI(TAG, "  最近概率: %.3f", s_last_prob);
    ESP_LOGI(TAG, "  门限: %.2f", s_threshold);
    ESP_LOGI(TAG, "  读游标句柄: %d", s_reader);
}
