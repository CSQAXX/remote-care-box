/*
 * ai_client.c —— 板子直连云端 AI 网关 (大模型语音对话)
 *
 * 见 ai_client.h 的详细设计说明。
 *
 * 实现要点:
 *   1. 用 esp_http_client 走 HTTPS POST /v1/turn
 *      (固件已开 CONFIG_MBEDTLS_CERTIFICATE_BUNDLE_DEFAULT_FULL,
 *       系统根证书打包在固件里, 会校验服务器证书)
 *   2. 请求体是 G.711 A-law 原始字节 (Content-Type: application/octet-stream)
 *   3. 鉴权用 HMAC-SHA256, 签名三段: device_id|ts|sha256(body)
 *      -> 放 HTTP 头 X-Device-Id / X-Timestamp / X-Signature
 *   4. 响应体是 PCM16 16k 单声道, 直接灌 audio_board_play_mono()
 *   5. 播放期间静音上行 (半双工), 防自激
 *   6. 断线指数退避重连 (与 push_client 一致)
 */

#include "ai_client.h"

#include <inttypes.h>
#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "audio_board.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "g711.h"
#include "mbedtls/sha256.h"

static const char *TAG = "ai_client";

/* ---------------- 内部状态 ---------------- */

static TaskHandle_t      s_task = NULL;
static volatile bool     s_connected = false;
static volatile bool     s_running = false;
static volatile uint32_t s_backoff_ms = AI_BACKOFF_MIN_MS;
static volatile ai_state_t s_state = AI_STATE_IDLE;

static volatile uint32_t s_turns = 0;
static volatile uint32_t s_replies = 0;
static volatile uint32_t s_errors = 0;
static volatile uint32_t s_connect_fails = 0;
static volatile uint32_t s_last_reply_len = 0;

static char s_last_user[64];
static char s_last_reply[128];

/* 手动触发一轮对话的标志位 (自动唤醒也走同一条路径) */
static volatile bool s_turn_requested = false;

/* 本轮对话的起句绝对位置 (>=0 有效)。由 wake_word 命中时写入。 */
static volatile int64_t s_turn_start_pos = -1;

/* 互斥保护: 保证同一时刻只有一轮对话在跑 */
static SemaphoreHandle_t s_turn_mutex = NULL;

/* ---------------- 工具函数 ---------------- */

/**
 * @brief SHA256 便捷封装
 *
 * 为什么不用 esp_crc_sha256():
 *   esp_crc.h (esp_hw_support) 里【只有 CRC32】, 没有 SHA256。
 *   IDF 里 SHA256 在 mbedtls/sha256.h, 一次调用 mbedtls_sha256() 就够。
 *
 * 为什么不用 mbedtls 的 HMAC API:
 *   手写 HMAC 只是几十行, 省掉 mbedtls_md 的一堆依赖,
 *   而且只需要 SHA256 这一种摘要。
 */
static void ai_sha256(const uint8_t *data, size_t len, uint8_t out[32])
{
    mbedtls_sha256(data, len, out, 0);
}

/**
 * @brief 计算 HMAC-SHA256 (返回 32 字节)
 *
 * HMAC 定义: H((K ^ opad) || H((K ^ ipad) || msg))
 */
static void ai_hmac_sha256(const uint8_t *key, size_t key_len,
                           const uint8_t *msg, size_t msg_len,
                           uint8_t out[32])
{
    uint8_t k_ipad[64];
    uint8_t k_opad[64];
    uint8_t inner[32];

    memset(k_ipad, 0, sizeof(k_ipad));
    memset(k_opad, 0, sizeof(k_opad));

    if (key_len > 64) {
        /* 密钥太长先哈希一次 (HMAC 标准做法) */
        ai_sha256(key, key_len, k_ipad);
        memcpy(k_opad, k_ipad, 32);
    } else {
        memcpy(k_ipad, key, key_len);
        memcpy(k_opad, key, key_len);
    }

    for (int i = 0; i < 64; i++) {
        k_ipad[i] ^= 0x36;
        k_opad[i] ^= 0x5c;
    }

    /* inner = H(k_ipad || msg) */
    {
        mbedtls_sha256_context ctx;
        mbedtls_sha256_init(&ctx);
        mbedtls_sha256_starts(&ctx, 0);
        mbedtls_sha256_update(&ctx, k_ipad, 64);
        mbedtls_sha256_update(&ctx, msg, msg_len);
        mbedtls_sha256_finish(&ctx, inner);
        mbedtls_sha256_free(&ctx);
    }

    /* out = H(k_opad || inner) */
    {
        mbedtls_sha256_context ctx;
        mbedtls_sha256_init(&ctx);
        mbedtls_sha256_starts(&ctx, 0);
        mbedtls_sha256_update(&ctx, k_opad, 64);
        mbedtls_sha256_update(&ctx, inner, 32);
        mbedtls_sha256_finish(&ctx, out);
        mbedtls_sha256_free(&ctx);
    }
}

/**
 * @brief URL 解码 (服务器把中文放在 HTTP 头里, 做了 percent-encoding)
 */
static void ai_url_decode(const char *in, char *out, size_t out_cap)
{
    size_t o = 0;
    for (size_t i = 0; in[i] && o + 1 < out_cap; i++) {
        if (in[i] == '%' && in[i + 1] && in[i + 2]) {
            char hex[3] = {in[i + 1], in[i + 2], 0};
            out[o++] = (char)strtol(hex, NULL, 16);
            i += 2;
        } else if (in[i] == '+') {
            out[o++] = ' ';
        } else {
            out[o++] = in[i];
        }
    }
    out[o] = '\0';
}

/**
 * @brief 计算一句话的 RMS 能量
 */
static uint32_t ai_rms(const int16_t *pcm, int n)
{
    if (n <= 0) {
        return 0;
    }
    uint32_t sum = 0;
    for (int i = 0; i < n; i++) {
        int32_t v = pcm[i];
        sum += (uint32_t)(v * v);
    }
    return (uint32_t)sqrt((double)sum / (double)n);
}

/* 内部用的状态名字 (避免和对外的 ai_client_state_str 混淆) */
static const char *ai_client_state_name(ai_state_t st);

/**
 * @brief 设置状态并打日志
 */
static void ai_set_state(ai_state_t st)
{
    if (s_state != st) {
        ESP_LOGI(TAG, "状态: %s -> %s", ai_client_state_name(s_state),
                 ai_client_state_name(st));
        s_state = st;
    }
}

static const char *ai_client_state_name(ai_state_t st)
{
    switch (st) {
    case AI_STATE_IDLE:      return "空闲";
    case AI_STATE_LISTENING: return "聆听中";
    case AI_STATE_THINKING:  return "思考中";
    case AI_STATE_SPEAKING:  return "播放中";
    case AI_STATE_ERROR:     return "出错";
    default:                 return "?";
    }
}

/* ---------------- 采集一句话 ---------------- */

/*
 * 本轮对话用的麦克风读游标 (-1 = 还没申请)。
 *
 * 为什么不用 audio_board_read_mono():
 *   那是【破坏性读】, 只有一个读指针。唤醒词任务也在读麦克风,
 *   两边会各拿一半数据 -> 唤醒词识别失真 + 上传音频被截断。
 *   共享读通道给每个消费者一个独立游标, 互不影响。
 */
static int s_capture_reader = -1;

/**
 * @brief 采集一整句语音, 返回 A-law 字节数
 *
 * 端点检测 (VAD) 逻辑:
 *   - 连续 AI_START_FRAMES 帧有声 -> 认为起句 (抗突发噪声)
 *   - 起句前保留 AI_PREROLL_MS 的音频 (别吃掉第一个字)
 *   - 连续静音 AI_SILENCE_END_MS -> 认为说完
 *   - 超过 AI_MAX_UTTERANCE_SEC -> 强制截断
 *
 * @param out        A-law 输出缓冲
 * @param out_cap    缓冲容量 (字节)
 * @param start_pos  起句绝对位置 (>=0 有效)。>=0 时把游标定位到那里,
 *                   于是唤醒词之前接的那半句也在录音里。
 * @return 采到的 A-law 字节数, <0 表示失败
 */
static int ai_capture_utterance(uint8_t *out, int out_cap, int64_t start_pos)
{
    int16_t pcm[AI_FRAME_SAMPLES];
    int16_t preroll[AI_FRAME_SAMPLES * (AI_PREROLL_MS / AI_FRAME_MS + 2)];
    int preroll_frames = 0;
    const int preroll_cap = (int)(sizeof(preroll) / sizeof(preroll[0]));

    int filled = 0;         /* 已写入 out 的字节数 */
    int preroll_len = 0;    /* preroll 里攒了多少样本 */
    int speech_frames = 0;
    int silence_frames = 0;
    bool started = false;

    const int max_frames = (AI_MAX_UTTERANCE_SEC * 1000) / AI_FRAME_MS;
    const int silence_end_frames = AI_SILENCE_END_MS / AI_FRAME_MS;

    /* 定位到起句点 (唤醒词接续用) */
    if (start_pos >= 0 && s_capture_reader >= 0) {
        if (audio_board_reader_seek(s_capture_reader, start_pos, 0) == 0) {
            ESP_LOGI(TAG, "起句位置: %" PRId64 " (唤醒词接续)", start_pos);
        }
    }

    for (int f = 0; f < max_frames; f++) {
        int n = (s_capture_reader >= 0)
                    ? audio_board_read_mono_shared(s_capture_reader, pcm, AI_FRAME_SAMPLES)
                    : audio_board_read_mono(pcm, AI_FRAME_SAMPLES);
        if (n <= 0) {
            vTaskDelay(pdMS_TO_TICKS(AI_FRAME_MS));
            continue;
        }
        /* 补齐到整帧 (read_mono 可能返回不足一帧) */
        if (n < AI_FRAME_SAMPLES) {
            vTaskDelay(pdMS_TO_TICKS(AI_FRAME_MS - (n * 1000) / AI_SAMPLE_RATE));
        }

        uint32_t rms = ai_rms(pcm, n);
        bool voiced = (rms >= AI_RMS_THRESHOLD);

        if (!started) {
            /* 把最近的音频存进 preroll 环形区 */
            if (preroll_len < preroll_cap) {
                memcpy(&preroll[preroll_len], pcm, n * sizeof(int16_t));
                preroll_len += n;
                preroll_frames++;
            } else {
                /* 满了就整体左移一帧 (简单但够用, 一句最多 300 帧) */
                memmove(preroll, preroll + n,
                        (preroll_cap - n) * sizeof(int16_t));
                memcpy(&preroll[preroll_cap - n], pcm, n * sizeof(int16_t));
                preroll_len = preroll_cap;
            }

            if (voiced) {
                speech_frames++;
                if (speech_frames >= AI_START_FRAMES) {
                    started = true;
                    /* 把 preroll 补进正式缓冲 */
                    int cap_samples = out_cap;   /* A-law 1 字节 / 样本 */
                    int want = preroll_len;
                    if (want > cap_samples) {
                        want = cap_samples;
                    }
                    g711_alaw_encode_block(preroll, out, want);
                    filled = want;
                    ESP_LOGI(TAG, "起句 (能量 %" PRIu32 ", 预roll %d 样本)", rms, want);
                }
            } else {
                speech_frames = 0;
            }
            continue;
        }

        /* 已起句: 正式采集 */
        if (filled + n > out_cap) {
            ESP_LOGW(TAG, "音频缓冲满 (%d), 截断", out_cap);
            break;
        }
        g711_alaw_encode_block(pcm, out + filled, n);
        filled += n;

        if (voiced) {
            silence_frames = 0;
        } else {
            silence_frames++;
            if (silence_frames >= silence_end_frames) {
                ESP_LOGI(TAG, "说完 (共 %d 字节 A-law)", filled);
                break;
            }
        }
    }

    if (!started) {
        ESP_LOGI(TAG, "没等到有效语音");
        return -1;
    }
    return filled;
}

/* ---------------- 一轮对话 ---------------- */

/**
 * @brief 跑完一轮: 采集 -> 上传 -> 播放
 */
static esp_err_t ai_do_turn(int64_t start_pos)
{
    const int max_alaw = AI_MAX_UTTERANCE_SEC * AI_SAMPLE_RATE;  /* 1 字节/样本 */

    uint8_t *alaw = heap_caps_malloc(max_alaw, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (alaw == NULL) {
        ESP_LOGE(TAG, "内存不足 (%d 字节)", max_alaw);
        s_errors++;
        return ESP_ERR_NO_MEM;
    }

    ai_set_state(AI_STATE_LISTENING);
    int len = ai_capture_utterance(alaw, max_alaw, start_pos);
    if (len <= 0) {
        free(alaw);
        ai_set_state(AI_STATE_IDLE);
        return ESP_ERR_TIMEOUT;   /* 不是错误, 只是没人说话 */
    }

    /* ---- 构造签名 ---- */
    /*
     * SHA256 用 mbedtls_sha256() 而不是 esp_crc_sha256() ——
     * esp_crc.h 里只有 CRC32, 没有 SHA256 (之前写错了, 编译不过)。
     */
    uint8_t digest[32];
    ai_sha256(alaw, len, digest);

    char digest_hex[65];
    for (int i = 0; i < 32; i++) {
        snprintf(digest_hex + i * 2, 3, "%02x", digest[i]);
    }

    /*
     * 签名时间戳必须是【真实时间】(服务器允许 ±10 分钟偏差)。
     * 原来用 esp_timer_get_time()/1000 是"开机多少毫秒"(比如 654034),
     * 服务器拿它和 time.time()*1000 (1.7e12) 一比差太多,
     * 直接判 401 "时间戳过期" —— 盒子发了也白发。
     * 板子开机后会 SNTP 对时 (time_sync.c), 所以这里取系统时间。
     */
    struct timeval tv_now;
    gettimeofday(&tv_now, NULL);
    int64_t now_ms = (int64_t)tv_now.tv_sec * 1000 + tv_now.tv_usec / 1000;
    if (tv_now.tv_sec < 1700000000) {
        /* 还没对时 (系统时间还是编译时刻), 签名一定会被服务器拒 */
        ESP_LOGW(TAG, "系统时间尚未对时 (sec=%ld), 本次请求可能被拒",
                 (long)tv_now.tv_sec);
    }
    char sign_src[160];
    int sign_len = snprintf(sign_src, sizeof(sign_src), "%s|%lld|%s",
                            AI_DEVICE_ID, (long long)now_ms, digest_hex);

    uint8_t sig[32];
    ai_hmac_sha256((const uint8_t *)AI_DEVICE_SECRET, strlen(AI_DEVICE_SECRET),
                   (const uint8_t *)sign_src, sign_len, sig);
    char sig_hex[65];
    for (int i = 0; i < 16; i++) {   /* 截前 16 字节 = 32 个 hex 字符 */
        snprintf(sig_hex + i * 2, 3, "%02x", sig[i]);
    }

    /* ---- HTTPS POST ---- */
    char url[128];
    snprintf(url, sizeof(url), "https://%s:%d/v1/turn", AI_SERVER_HOST, AI_SERVER_PORT);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 60000,
        /* 用系统根证书包校验服务器证书 (语音内容不能走明文) */
        .crt_bundle_attach = esp_crt_bundle_attach,
    };
    esp_http_client_handle_t cli = esp_http_client_init(&cfg);
    if (cli == NULL) {
        ESP_LOGE(TAG, "http client 初始化失败");
        free(alaw);
        s_errors++;
        ai_set_state(AI_STATE_ERROR);
        return ESP_FAIL;
    }

    char hdr_ts[32];
    snprintf(hdr_ts, sizeof(hdr_ts), "%lld", (long long)now_ms);
    esp_http_client_set_header(cli, "X-Device-Id", AI_DEVICE_ID);
    esp_http_client_set_header(cli, "X-Timestamp", hdr_ts);
    esp_http_client_set_header(cli, "X-Signature", sig_hex);
    esp_http_client_set_header(cli, "Content-Type", "application/octet-stream");

    esp_err_t ret = esp_http_client_open(cli, len);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "连接服务器失败: %s", esp_err_to_name(ret));
        esp_http_client_cleanup(cli);
        free(alaw);
        s_errors++;
        s_connect_fails++;
        ai_set_state(AI_STATE_ERROR);
        return ret;
    }

    s_connected = true;

    /* 分块发送 (每块 4KB) */
    int sent = 0;
    while (sent < len) {
        int chunk = (len - sent > 4096) ? 4096 : (len - sent);
        int w = esp_http_client_write(cli, (const char *)(alaw + sent), chunk);
        if (w < 0) {
            ESP_LOGE(TAG, "发送失败: w=%d errno=%d 已发 %d/%d", w, errno, sent, len);
            esp_http_client_close(cli);
            esp_http_client_cleanup(cli);
            free(alaw);
            s_errors++;
            s_connected = false;
            ai_set_state(AI_STATE_ERROR);
            return ESP_FAIL;
        }
        sent += w;
    }
    free(alaw);

    /* 结束请求, 等服务器返回 */
    int content_len = esp_http_client_fetch_headers(cli);
    int status = esp_http_client_get_status_code(cli);
    if (status != 200) {
        ESP_LOGE(TAG, "服务器返回 %d", status);
        esp_http_client_close(cli);
        esp_http_client_cleanup(cli);
        s_errors++;
        ai_set_state(AI_STATE_ERROR);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "服务器已应答, 音频 %d 字节", content_len);

    /* ---- 播放回复 ---- */
    ai_set_state(AI_STATE_THINKING);

    /*
     * 读响应头里的文本 (URL 编码的中文)。
     *
     * esp_http_client_get_header() 的签名是 (client, key, char **value),
     * 第三个参数是【char **】—— 它会返回一个新分配的字符串。
     *
     * 注意: IDF 5.3 没有 esp_http_client_free_header_value(),
     * 直接用 free() 释放 (mbedtls 内部用 calloc 分配的)。
     * (第一版按 "get_header(cli, key, buf, len)" 写, 编译不过。)
     */
    char *user_hdr = NULL;
    char *reply_hdr = NULL;
    esp_http_client_get_header(cli, "X-User-Text", &user_hdr);
    esp_http_client_get_header(cli, "X-Reply-Text", &reply_hdr);
    if (user_hdr) {
        ai_url_decode(user_hdr, s_last_user, sizeof(s_last_user));
        free(user_hdr);
    }
    if (reply_hdr) {
        ai_url_decode(reply_hdr, s_last_reply, sizeof(s_last_reply));
        free(reply_hdr);
    }
    ESP_LOGI(TAG, "识别: %s", s_last_user);
    ESP_LOGI(TAG, "回复: %s", s_last_reply);

    /*
     * 播放期间静音上行 (半双工)。
     *
     * 喇叭的声音会被麦克风收回去, 如果这时还在上传音频,
     * 服务器会以为老人又说话了, 形成自问自答的循环。
     * audio_board_set_downlink_active() 让采集侧静音, 双保险。
     */
    audio_board_set_downlink_active(true);
    ai_set_state(AI_STATE_SPEAKING);

    /* PCM16 16k 单声道, 分块读分块播 */
    int16_t pcm[AI_FRAME_SAMPLES * 8];
    int total_read = 0;
    while (1) {
        int r = esp_http_client_read(cli, (char *)pcm, sizeof(pcm));
        if (r <= 0) {
            break;
        }
        int samples = r / sizeof(int16_t);
        audio_board_play_mono(pcm, samples);
        total_read += r;
    }

    audio_board_set_downlink_active(false);

    s_last_reply_len = total_read;
    s_replies++;
    s_turns++;

    esp_http_client_close(cli);
    esp_http_client_cleanup(cli);

    ESP_LOGI(TAG, "播放完成 (%d 字节 PCM16)", total_read);
    ai_set_state(AI_STATE_IDLE);
    return ESP_OK;
}

/* ---------------- 后台任务 ---------------- */

static void ai_task(void *arg)
{
    (void)arg;

    while (s_running) {
        if (!s_turn_requested) {
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }

        s_turn_requested = false;
        int64_t start_pos = s_turn_start_pos;
        s_turn_start_pos = -1;

        /* 同一时刻只跑一轮 */
        if (xSemaphoreTake(s_turn_mutex, pdMS_TO_TICKS(100)) != pdTRUE) {
            continue;
        }

        /*
         * 申请独立的麦克风读游标。
         *
         * 必须用共享读通道而不是 audio_board_read_mono() ——
         * 后者是破坏性读, 唤醒词任务也在读, 两边会互相抢数据。
         */
        if (s_capture_reader < 0) {
            s_capture_reader = audio_board_reader_open();
            if (s_capture_reader < 0) {
                ESP_LOGE(TAG, "申请共享读通道失败");
                s_errors++;
                xSemaphoreGive(s_turn_mutex);
                vTaskDelay(pdMS_TO_TICKS(AI_BACKOFF_MIN_MS));
                continue;
            }
        }

        esp_err_t ret = ai_do_turn(start_pos);

        xSemaphoreGive(s_turn_mutex);

        if (ret == ESP_FAIL || ret == ESP_ERR_NO_MEM) {
            /* 出错: 退避后重试, 但不无限重试同一次失败 */
            s_connected = false;
            s_backoff_ms = AI_BACKOFF_MIN_MS;
            vTaskDelay(pdMS_TO_TICKS(AI_BACKOFF_MIN_MS));
        }
    }

    if (s_capture_reader >= 0) {
        audio_board_reader_close(s_capture_reader);
        s_capture_reader = -1;
    }
    s_task = NULL;
    vTaskDelete(NULL);
}

/* ---------------- 对外接口 ---------------- */

esp_err_t ai_client_start(void)
{
    if (s_running) {
        return ESP_OK;
    }

    s_turn_mutex = xSemaphoreCreateMutex();
    if (s_turn_mutex == NULL) {
        ESP_LOGE(TAG, "互斥锁创建失败");
        return ESP_FAIL;
    }

    s_running = true;
    s_state = AI_STATE_IDLE;

    /*
     * 栈必须 >= 32KB, 不能用 8192。
     *
     * ai_capture_utterance() 一进函数就在栈上放
     *   int16_t preroll[320 * (300/20 + 2)] = 5440 样本 = 10880 字节
     *   int16_t pcm[320]                    =            640 字节
     * 单这一帧就 11520 字节, 已超过原来的 8192 字节任务栈;
     * ai_play_response() 还有 pcm[2560] = 5120 字节, 加上
     * esp_http_client / mbedTLS 握手本身的栈开销。
     *
     * 实测后果 (2026-11-08 17:13 串口日志): 喊中「晴老师」后
     * `状态: 空闲 -> 聆听中` 立刻
     * `***ERROR*** A stack overflow in task ai_client` -> Rebooting。
     * 之前开机一直崩在 MultiNet 的 build_fsts, 从没走到这条路径,
     * 所以这个栈溢出一直潜伏到唤醒词修好才暴露。
     *
     * 堆余量充足 (实测内部堆约 3.8MB 空闲), 多给 24KB 无压力。
     */
    xTaskCreate(ai_task, "ai_client", 32768, NULL, 4, &s_task);
    if (s_task == NULL) {
        s_running = false;
        vSemaphoreDelete(s_turn_mutex);
        s_turn_mutex = NULL;
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "AI 对话客户端已启动 (服务器 %s:%d)", AI_SERVER_HOST, AI_SERVER_PORT);
    return ESP_OK;
}

void ai_client_stop(void)
{
    s_running = false;
    s_turn_requested = false;
    /* 任务自己会退出并清 s_task, 这里不强杀 (避免打断 esp_http_client) */
}

bool ai_client_is_connected(void)
{
    return s_connected;
}

ai_state_t ai_client_get_state(void)
{
    return s_state;
}

const char *ai_client_state_str(void)
{
    return ai_client_state_name(s_state);
}

esp_err_t ai_client_start_turn(void)
{
    if (!s_running) {
        return ESP_ERR_INVALID_STATE;
    }
    s_turn_start_pos = -1;
    s_turn_requested = true;
    return ESP_OK;
}

esp_err_t ai_client_start_turn_at(int64_t start_pos)
{
    if (!s_running) {
        return ESP_ERR_INVALID_STATE;
    }
    s_turn_start_pos = start_pos;
    s_turn_requested = true;
    return ESP_OK;
}

void ai_client_get_stats(ai_client_stats_t *out)
{
    if (out == NULL) {
        return;
    }
    memset(out, 0, sizeof(*out));
    out->turns = s_turns;
    out->replies = s_replies;
    out->errors = s_errors;
    out->connect_fails = s_connect_fails;
    out->last_reply_len = s_last_reply_len;
    out->backoff_ms = s_backoff_ms;
    out->connected = s_connected;
    out->state = s_state;
    snprintf(out->last_user, sizeof(out->last_user), "%s", s_last_user);
    snprintf(out->last_reply, sizeof(out->last_reply), "%s", s_last_reply);
}

const char *ai_client_last_user_text(void)
{
    return s_last_user;
}

const char *ai_client_last_reply_text(void)
{
    return s_last_reply;
}
