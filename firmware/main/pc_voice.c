/*
 * pc_voice.c —— 「小电脑」: 喊一声 -> 电脑干活 -> 板子念回话
 *
 * 见 pc_voice.h 的设计说明。这里是【全新文件】, 不改任何现有源码。
 *
 * 一轮的完整路径:
 *   唤醒词 id=316
 *     -> pc_voice_start_turn_at(start_pos)         (异步, 懒初始化)
 *     -> 独立读游标从 start_pos 录一句 G.711 A-law
 *     -> HTTP POST http://<电脑>:8767/voice
 *          X-IoT-Token / X-Audio-Codec: g711a / X-Want-Audio: 1
 *     -> 电脑: 本地 ASR -> 快捷路由/本地模型 -> 真开软件
 *     -> 回包: body = PCM16 16k 中文语音, 头 = X-User-Text / X-Reply-Text
 *     -> 屏幕显示「听到 / 电脑」两行, 喇叭把回话念出来
 *
 * 失败时【绝不假报成功】: 连不上、口令错、电脑没理, 一律显示原因 +
 * 本地低音提示, 不播「开好了」。TTS 挂了电脑端会自己降级成「叮咚」。
 */

#include "pc_voice.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <errno.h>
#include <unistd.h>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>

#include "ai_client.h"          /* 只看 ai_client_get_state(), 绝不改它的状态 */
#include "audio_board.h"
#include "g711.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "g711.h"
#include "lcd_prompt.h"
#include "nvs.h"

/* 电脑 IP / 口令由 enable_pc_voice.py 生成到 pc_voice_config.h。
   没生成也不影响编译 (兜底空口令 = 不启用, 不会误连)。 */
#if defined(__has_include)
#  if __has_include("pc_voice_config.h")
#    include "pc_voice_config.h"
#  endif
#endif
#ifndef PC_VOICE_HOST
#  define PC_VOICE_HOST  "192.168.2.135"
#endif
#ifndef PC_VOICE_PORT
#  define PC_VOICE_PORT  8767
#endif
#ifndef PC_VOICE_TOKEN
#  define PC_VOICE_TOKEN ""
#endif

#define PC_TAG              "pc_voice"
#define PC_SAMPLE_RATE      16000
#define PC_FRAME_MS         20
#define PC_FRAME_SAMPLES    (PC_SAMPLE_RATE * PC_FRAME_MS / 1000)  /* 320 */
#define PC_MAX_SEC          10
#define PC_SILENCE_END_MS   500   /* 原来 900: 说完还要白录 0.9 秒, 8k 下就是 7KB 白传 */
#define PC_UPLINK_RATE      8000  /* 上传按 8kHz: 电话音质, 识别够用, 数据量砍半 */
#define PC_UPLINK_MAX       14000 /* 一次能推完的上限 (TCP 发送缓冲 16384, 留余量) */
#define PC_PREROLL_MS       300
#define PC_START_FRAMES     3
#define PC_RMS_THRESHOLD    350
#define PC_HTTP_TIMEOUT_MS  15000   /* 电脑端 1 秒左右就回, 15 秒足够; 短一点失败才看得见 */
#define PC_HEAD_BUF         4096    /* 响应头缓冲: 中文回话 URL 编码后一个汉字 9 字节 */
#define PC_TEXT_BUF         512     /* 用户说的话 (URL 编码后) */
#define PC_REPLY_BUF        1536    /* 电脑回话 (URL 编码后, 约 170 个汉字) */
#define PC_TASK_STACK       8192   /* 大数组已挪到静态区, 8KB 够用 */

/*
 * pc_http_turn() 的两种失败必须分开:
 *   PC_ERR_RETRY    —— 连接/发送阶段就断了, 电脑肯定没收到, 重发是安全的
 *   PC_ERR_GAVE_UP  —— 音频已经整段发出去了, 只是没等到回话。
 *                      这时电脑**可能已经执行了**(比如微信已经打开),
 *                      绝不能重发, 否则会重复执行一次。
 */
#define PC_ERR_RETRY        (-1)
#define PC_ERR_GAVE_UP      (-2)

static const char *TAG = PC_TAG;

static SemaphoreHandle_t s_busy = NULL;
static SemaphoreHandle_t s_wake_sig = NULL;   /* 唤醒本模块工作线程 */
static TaskHandle_t      s_worker = NULL;     /* 常驻工作线程 (开机建好, 复用) */
static volatile bool     s_running = false;
static volatile int64_t  s_next_start = -1;
static portMUX_TYPE      s_init_lock = portMUX_INITIALIZER_UNLOCKED;

/* ---------------- 配置 ---------------- */

static void pc_load_config(char *host, size_t hcap, char *tok, size_t tcap)
{
    snprintf(host, hcap, "%s", PC_VOICE_HOST);
    snprintf(tok, tcap, "%s", PC_VOICE_TOKEN);

    /* NVS 覆盖优先: 电脑换 IP / 换口令时不用重烧 */
    nvs_handle_t nh;
    if (nvs_open("pcvoice", NVS_READONLY, &nh) == ESP_OK) {
        char buf[128];
        size_t n = sizeof(buf);
        if (nvs_get_str(nh, "host", buf, &n) == ESP_OK && buf[0]) {
            snprintf(host, hcap, "%s", buf);
        }
        n = sizeof(buf);
        if (nvs_get_str(nh, "token", buf, &n) == ESP_OK && buf[0]) {
            snprintf(tok, tcap, "%s", buf);
        }
        nvs_close(nh);
    }
}

esp_err_t pc_voice_set_config(const char *host, const char *token)
{
    nvs_handle_t nh;
    esp_err_t e = nvs_open("pcvoice", NVS_READWRITE, &nh);
    if (e != ESP_OK) {
        return e;
    }
    if (host && host[0]) {
        nvs_set_str(nh, "host", host);
    }
    if (token && token[0]) {
        nvs_set_str(nh, "token", token);
    }
    e = nvs_commit(nh);
    nvs_close(nh);
    ESP_LOGI(TAG, "配置已保存到 NVS (host=%s)", host ? host : "(不变)");
    return e;
}

/* ---------------- 小工具 ---------------- */

/* 响应头里的中文是 URL 编码的, HTTP 头不能带非 ASCII */
static bool pc_is_hex(char c)
{
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

static void pc_url_decode(const char *in, char *out, size_t cap)
{
    size_t o = 0;
    for (size_t i = 0; in[i]; i++) {
        unsigned char b;

        if (in[i] == '%' && in[i + 1] && in[i + 2] &&
            pc_is_hex(in[i + 1]) && pc_is_hex(in[i + 2])) {
            char hex[3] = {in[i + 1], in[i + 2], 0};
            b = (unsigned char)strtol(hex, NULL, 16);
            i += 2;
        } else if (in[i] == '+') {
            b = ' ';
        } else {
            b = (unsigned char)in[i];
        }

        /*
         * 【别把汉字切一半】
         * 头被截断时, 最后一个 "%E7" 之类可能只解出汉字 3 个字节里的第 1 个。
         * 这种半个字符画到屏上就是一个方块, 日志里是个乱码符号。
         * 所以先看这个字节是几字节字符的开头, 剩余容量装不下整个字符
         * 就到此为止 —— 宁可少显示一个字, 也不要显示半个。
         */
        int need = 1;
        if (b >= 0xF0) {
            need = 4;
        } else if (b >= 0xE0) {
            need = 3;
        } else if (b >= 0xC0) {
            need = 2;
        }
        if (o + (size_t)need >= cap) {
            break;
        }
        out[o++] = (char)b;
    }
    out[o] = '\0';
}

static uint32_t pc_rms(const int16_t *pcm, int n)
{
    if (n <= 0) {
        return 0;
    }
    uint64_t sum = 0;
    for (int i = 0; i < n; i++) {
        int32_t v = pcm[i];
        sum += (uint64_t)((int64_t)v * v);
    }
    return (uint32_t)sqrt((double)sum / (double)n);
}

/* 连不上电脑 / 电脑报错时, 板子自己发一声, 别让人以为没反应 */
static void pc_local_beep(bool ok)
{
    const int n = PC_SAMPLE_RATE / 4;      /* 0.25 秒 */
    /* 【改】放 PSRAM, 不占内部 RAM 也不占任务栈。
     * 内部 RAM 太紧: 放栈上 -> xTaskCreate 失败; 放静态区 -> 挤爆开机任务。
     * 表现都是"喊小电脑没反应", 而且连带 ai_client 发送也失败。 */
    int16_t *pcm = heap_caps_malloc(sizeof(int16_t) * n,
                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pcm == NULL) {
        return;
    }
    const double pi = 3.14159265358979323846;
    for (int i = 0; i < n; i++) {
        double t = (double)i / PC_SAMPLE_RATE;
        double freq = ok ? (t < 0.10 ? 880.0 : 1320.0) : 440.0;
        double env = fmin(1.0, fmin(t / 0.01, (0.25 - t) / 0.03));
        if (env < 0) {
            env = 0;
        }
        pcm[i] = (int16_t)(5200.0 * env * sin(2 * pi * freq * t));
    }
    audio_board_set_downlink_active(true);
    audio_board_play_mono(pcm, n);
    audio_board_set_downlink_active(false);
    free(pcm);
}

static int pc_utf8_bytes(unsigned char c)
{
    if (c < 0x80) {
        return 1;
    }
    if ((c & 0xE0) == 0xC0) {
        return 2;
    }
    if ((c & 0xF0) == 0xE0) {
        return 3;
    }
    if ((c & 0xF8) == 0xF0) {
        return 4;
    }
    return 1;                       /* 非法起始字节, 当单字节跳过 */
}

/*
 * 把一句话裁成屏幕上放得下的长度。
 *
 * 【为什么必须裁】
 * 1) 屏幕 320 像素宽, 每字 17 像素 -> 一行只放得下 18 个汉字。
 *    电脑回的话常常 40~60 字 (「已帮你打开本机 微信 客户端（微信），
 *    窗口可能还在启动…」), 直接给会被切在屏幕右边缘。
 * 2) lcd_prompt 内部缓冲是 64 字节, 按字节硬截会把一个汉字切成半个,
 *    屏幕上就出现乱码方块 —— 这就是「字幕有问题」的来源。
 * 所以这里按 UTF-8 字符边界裁到 max_chars 个字, 后面接省略号。
 */
/* 一行最多 17 个汉字 (屏幕 320px, 每字 17px, 两侧各留一点边) */
#define PC_LINE_CHARS   17

/*
 * 把回话排成【两行】字幕, 行间用一个 '\n' 分开。
 *
 * 为什么是两行:
 *   横幅 40 像素高, 正好放两行 16px 字。一行只能塞 17 个字, 电脑
 *   一开口就是二三十个字, 只显示一行的话后半句永远看不到。
 *   两行 = 34 个字, 日常回话基本都能完整显示。
 *
 * 按 UTF-8 字符边界切 (不是按字节), 所以绝不会把一个汉字切成两半
 * 变出乱码方块。真超长就在第二行末尾补「…」。
 *
 * 只写 dst, 不改 src —— src 还要留着打日志和念。
 */
static void pc_fit_two_lines(const char *src, char *dst, size_t cap)
{
    size_t o = 0;
    int total = 0;      /* 已排进去的字符总数 */
    int in_line = 0;    /* 当前这一行已排几个字 */
    const char *p = src;

    dst[0] = '\0';
    while (p != NULL && *p && total < PC_LINE_CHARS * 2) {
        unsigned char c = (unsigned char)*p;

        /*
         * 电脑回话里可能夹着换行 / 制表符 (LLM 爱用列表格式), 也可能带
         * markdown 的 **加粗** 、# 标题 、`代码` 这些符号。
         * 它们在屏幕上一没字形二没意义, 留着只会让字幕变脏,
         * 换行符还会让 draw_banner 提前分行。
         * 统一跳过, 由我们按 17 字自己决定在哪里折行。
         */
        if (c == '\n' || c == '\r' || c == '\t' ||
            c == '*' || c == '#' || c == '`') {
            p++;
            continue;
        }

        int l = pc_utf8_bytes(c);
        if (l <= 0) {
            break;
        }
        /* 留 4 字节余量: 3 字节省略号 + 结尾 '\0' */
        if (o + (size_t)l + 4 >= cap) {
            break;
        }
        if (in_line >= PC_LINE_CHARS) {
            dst[o++] = '\n';            /* 换到第二行 */
            in_line = 0;
        }
        memcpy(dst + o, p, l);
        o += (size_t)l;
        p += l;
        total++;
        in_line++;
    }
    if (p != NULL && *p && o + 3 < cap) {   /* 还有没排完的 -> 省略号 */
        memcpy(dst + o, "\xE2\x80\xA6", 3);
        o += 3;
    }
    dst[o] = '\0';
}

/*
 * 屏幕字幕: 白底黑字, 像电视字幕, 白天黑夜都看得清。
 *
 * 用 PROMPT_PC 而不用 PROMPT_CONTENT: 类型不同才会先擦旧横幅,
 * 也就不会和老人看护的提示 (黑底黄字) 串到一起, 更不会改到老人那边。
 */
#define PC_SUBTITLE_FG   0x0000   /* 黑字 */
#define PC_SUBTITLE_BG   0xFFFF   /* 白底 */

static void pc_show(const char *text, uint32_t ms)
{
    char fitted[128];               /* 两行 34 字 + 换行 + 省略号 */
    pc_fit_two_lines(text, fitted, sizeof(fitted));
    ESP_LOGI(TAG, "%s", text);      /* 日志留全文, 方便排查 */
    lcd_prompt_show_styled(PROMPT_PC, fitted, ms, PC_SUBTITLE_FG, PC_SUBTITLE_BG);
}

/* ---------------- 录一句 ---------------- */

/*
 * 和 ai_client.c 的 ai_capture_utterance() 同款逻辑, 但用【自己的读游标】:
 * audio_board 的共享读通道支持多个 reader, 各读各的互不抢数据。
 * 这里不复用 ai_client 的静态函数, 是为了一个字节都不改它。
 */
static int pc_capture(uint8_t *out, int out_cap, int reader, int64_t start_pos)
{
    /* 【改】两块缓冲都放 PSRAM, 任务栈只要几百字节 */
    int16_t *pcm = heap_caps_malloc(sizeof(int16_t) * PC_FRAME_SAMPLES,
                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    const int preroll_cap = PC_FRAME_SAMPLES * (PC_PREROLL_MS / PC_FRAME_MS + 2);
    int16_t *preroll = heap_caps_malloc(sizeof(int16_t) * preroll_cap,
                                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pcm == NULL || preroll == NULL) {
        free(pcm);
        free(preroll);
        ESP_LOGE(TAG, "采集缓冲申请失败");
        return -1;
    }
    int preroll_len = 0;
    int filled = 0, speech_frames = 0, silence_frames = 0;
    bool started = false;

    const int max_frames = (PC_MAX_SEC * 1000) / PC_FRAME_MS;
    const int silence_end_frames = PC_SILENCE_END_MS / PC_FRAME_MS;

    if (start_pos >= 0 && reader >= 0) {
        if (audio_board_reader_seek(reader, start_pos, 0) == 0) {
            ESP_LOGI(TAG, "起句位置 %" PRId64 " (唤醒词接续)", start_pos);
        }
    }

    for (int f = 0; f < max_frames; f++) {
        int n = (reader >= 0)
                    ? audio_board_read_mono_shared(reader, pcm, PC_FRAME_SAMPLES)
                    : audio_board_read_mono(pcm, PC_FRAME_SAMPLES);
        if (n <= 0) {
            vTaskDelay(pdMS_TO_TICKS(PC_FRAME_MS));
            continue;
        }
        if (n < PC_FRAME_SAMPLES) {
            vTaskDelay(pdMS_TO_TICKS(PC_FRAME_MS - (n * 1000) / PC_SAMPLE_RATE));
        }

        uint32_t rms = pc_rms(pcm, n);
        bool voiced = (rms >= PC_RMS_THRESHOLD);

        if (!started) {
            if (preroll_len < preroll_cap) {
                memcpy(&preroll[preroll_len], pcm, n * sizeof(int16_t));
                preroll_len += n;
            } else {
                memmove(preroll, preroll + n,
                        (preroll_cap - n) * sizeof(int16_t));
                memcpy(&preroll[preroll_cap - n], pcm, n * sizeof(int16_t));
                preroll_len = preroll_cap;
            }
            if (voiced) {
                if (++speech_frames >= PC_START_FRAMES) {
                    started = true;
                    int want = preroll_len > out_cap ? out_cap : preroll_len;
                    g711_alaw_encode_block(preroll, out, want);
                    filled = want;
                    ESP_LOGI(TAG, "起句 (能量 %" PRIu32 ", 预roll %d 样本)", rms, want);
                }
            } else {
                speech_frames = 0;
            }
            continue;
        }

        if (filled + n > out_cap) {
            ESP_LOGW(TAG, "音频缓冲满, 截断");
            break;
        }
        g711_alaw_encode_block(pcm, out + filled, n);
        filled += n;

        if (voiced) {
            silence_frames = 0;
        } else if (++silence_frames >= silence_end_frames) {
            ESP_LOGI(TAG, "说完 (共 %d 字节 A-law)", filled);
            break;
        }
    }
    free(pcm);
    free(preroll);
    return started ? filled : -1;
}

/* ---------------- 直连电脑: 原始 socket ---------------- */

/*
 * 【为什么不再用 esp_http_client】
 * 它内部靠 select() 等「socket 可写」。在这条 Wi-Fi 链路上这个等待不可靠:
 * 实测每发 4~8KB 之后就 select 超时一次, 而超时时长正好等于设的 timeout_ms,
 * 于是整轮永远停在「发了一半」。
 * 换成阻塞 socket: send() 该等就等, ACK 到了立刻继续, 完全不碰 select。
 */

static bool pc_send_all(int s, const char *buf, int len, int budget_ms, bool smooth)
{
    int sent = 0;
    int next_tick = 8192;
    int64_t deadline = esp_timer_get_time() + (int64_t)budget_ms * 1000;
    while (sent < len) {
        /*
         * 【平滑发送】一次 send() 把整段塞给 lwIP, lwIP 会一股脑丢给
         * Wi-Fi 驱动, 在 2.4G 拥挤信道上直接雪崩式丢包 —— 实测 52 秒
         * 只推进了 22KB。改成每次最多 1KB、块间让 8ms, 速率约 125KB/s,
         * 对这条链路刚好, 丢包少了总耗时反而更短。
         */
        int chunk = len - sent;
        int cap = smooth ? 1024 : chunk;
        if (chunk > cap) {
            chunk = cap;
        }
        int n = send(s, buf + sent, chunk, 0);
        if (n > 0) {
            sent += n;
            if (sent >= next_tick) {
                ESP_LOGI(TAG, "  发送进度 %d/%d", sent, len);
                next_tick += 8192;
            }
            if (smooth) {
                vTaskDelay(pdMS_TO_TICKS(8));
            }
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (esp_timer_get_time() > deadline) {
                ESP_LOGE(TAG, "发不动 (已发 %d/%d, %d ms 没进展)", sent, len, budget_ms);
                return false;
            }
            continue;
        }
        ESP_LOGE(TAG, "send 出错: n=%d errno=%d (已发 %d/%d)", n, errno, sent, len);
        return false;
    }
    return true;
}

/* 从响应头里取一个字段 (值是 URL 编码的) */
static void pc_hdr_find(const char *block, const char *name, char *out, size_t cap)
{
    size_t nl = strlen(name);
    const char *p = block;
    out[0] = '\0';
    while (p != NULL && *p) {
        if (strncasecmp(p, name, nl) == 0 && p[nl] == ':') {
            const char *v = p + nl + 1;
            while (*v == ' ') {
                v++;
            }
            char raw[256];
            size_t i = 0;
            while (v[i] && v[i] != '\r' && v[i] != '\n' && i + 1 < sizeof(raw)) {
                raw[i] = v[i];
                i++;
            }
            raw[i] = '\0';
            pc_url_decode(raw, out, cap);
            return;
        }
        const char *q = strstr(p, "\r\n");
        if (q == NULL) {
            break;
        }
        p = q + 2;
    }
}

/*
 * 一轮: 发 A-law -> 边收 PCM16 边播 -> 返回 HTTP 状态码 (传输层失败 -1)
 */
static int pc_http_turn(const char *host, const char *tok,
                        const uint8_t *body, int len,
                        char *user_out, size_t ucap,
                        char *reply_out, size_t rcap)
{
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(PC_VOICE_PORT);
    addr.sin_addr.s_addr = inet_addr(host);
    if (addr.sin_addr.s_addr == INADDR_NONE) {
        ESP_LOGE(TAG, "电脑地址不合法: %s", host);
        return PC_ERR_RETRY;
    }

    int s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s < 0) {
        ESP_LOGE(TAG, "建 socket 失败: errno=%d", errno);
        return PC_ERR_RETRY;
    }
    /*
     * 【超时必须分开设】
     * 发送方向的 10 秒够用 (33KB 实测 12 秒发完, 留到 40 秒余量见下面 budget)。
     * 接收方向原来也是 10 秒 —— 这是「电脑明明打开了微信、盒子却说没回话」的
     * 真正原因: 电脑那边读完音频要 ~19 秒、再加上识别+执行+合成回话,
     * 整轮实测 20.7 秒才吐响应, 盒子等 10 秒就放弃了。
     * 收到响应头之前最多等 45 秒。
     */
    struct timeval tvs = {.tv_sec = 10, .tv_usec = 0};
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tvs, sizeof(tvs));
    struct timeval tvr = {.tv_sec = 45, .tv_usec = 0};
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tvr, sizeof(tvr));

    esp_wifi_set_ps(WIFI_PS_NONE);   /* 传的时候别睡, 睡了 ACK 就收不及时 */

    int64_t t0 = esp_timer_get_time();
    if (connect(s, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        ESP_LOGE(TAG, "连电脑失败: errno=%d", errno);
        close(s);
        esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
        return PC_ERR_RETRY;
    }
    ESP_LOGI(TAG, "连上 %s:%d (%lld ms)", host, PC_VOICE_PORT,
             (esp_timer_get_time() - t0) / 1000);

    char hdr[512];
    int hlen = snprintf(hdr, sizeof(hdr),
                        "POST /voice HTTP/1.1\r\n"
                        "Host: %s:%d\r\n"
                        "User-Agent: kanhu-box\r\n"
                        "X-IoT-Token: %s\r\n"
                        "Content-Type: application/octet-stream\r\n"
                        "X-Audio-Codec: g711a\r\n"
                        "X-Sample-Rate: %d\r\n"
                        "X-Want-Audio: 1\r\n"
                        "X-Source: esp32-kanhu\r\n"
                        "Content-Length: %d\r\n"
                        "Connection: close\r\n\r\n",
                        host, PC_VOICE_PORT, tok, PC_UPLINK_RATE, len);
    if (hlen <= 0 || hlen >= (int)sizeof(hdr)) {
        ESP_LOGE(TAG, "请求头太长");
        close(s);
        esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
        return PC_ERR_RETRY;
    }

    ESP_LOGI(TAG, "发 %d 字节 A-law", len);
    int64_t t_send = esp_timer_get_time();
    if (!pc_send_all(s, hdr, hlen, 8000, false) ||
        !pc_send_all(s, (const char *)body, len, 60000, true)) {
        close(s);
        esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
        return PC_ERR_RETRY;      /* 没发出去, 重发安全 */
    }
    ESP_LOGI(TAG, "请求发完, 等电脑回话 (上传耗时 %lld ms)",
             (esp_timer_get_time() - t_send) / 1000);
    /* 电脑那边处理要 ~20 秒, 屏幕上得有个交代, 不然用户以为死机了 */
    pc_show("电脑在想…", 20000);

    /*
     * 响应头缓冲放 4KB。
     *
     * 原来只有 1024 字节, 但电脑把识别文本和回话放在 X-User-Text /
     * X-Reply-Text 两个头里, 中文还要 URL 编码 (一个汉字 9 字节)。
     * 一句 50 字的回话就是 450 字节, 加上 Content-Length 等其它头,
     * 很容易顶到 1KB —— 头没收全就找不到 "\r\n\r\n", 整轮白等。
     * 放 PSRAM, 不占内部 RAM。
     */
    char *rbuf = heap_caps_malloc(PC_HEAD_BUF, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (rbuf == NULL) {
        close(s);
        esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
        return PC_ERR_GAVE_UP;     /* 已经发出去了, 别重发 */
    }
    /*
     * 等响应。电脑读完音频会先回一个标准的 "HTTP/1.1 100 Continue",
     * 这是「整段音频已收到」的确认 —— 有了它板子才敢安心等结果;
     * 一个字节都收不到, 才说明包在路上丢了, 这时重发是安全的
     * (电脑没读到正文就不会执行任何动作)。
     */
    int rlen = 0;
    char *hend = NULL;
    bool acked = false;
    while (1) {
        while (rlen < PC_HEAD_BUF - 1) {
            int n = recv(s, rbuf + rlen, PC_HEAD_BUF - 1 - rlen, 0);
            if (n <= 0) {
                break;
            }
            rlen += n;
            rbuf[rlen] = '\0';
            hend = strstr(rbuf, "\r\n\r\n");
            if (hend != NULL) {
                break;
            }
        }
        rbuf[rlen] = '\0';
        if (hend == NULL) {
            break;                      /* 超时或对端关闭 */
        }
        if (!acked && rlen >= 12 && strncmp(rbuf, "HTTP/1.1 100", 12) == 0) {
            acked = true;
            int used = (int)(hend - rbuf) + 4;
            int rest = rlen - used;
            if (rest > 0) {
                memmove(rbuf, rbuf + used, rest);
            }
            rlen = rest;
            rbuf[rlen] = '\0';
            hend = NULL;
            ESP_LOGI(TAG, "电脑已收到音频, 开始处理");
            pc_show("电脑收到了, 正在办…", 40000);
            continue;
        }
        break;
    }
    if (hend == NULL && !acked && rlen == 0) {
        /* 连确认都没收到: 音频根本没到电脑, 重发安全 */
        ESP_LOGW(TAG, "音频没送到电脑 (一个字节都没回), 可以重发");
        free(rbuf);
        close(s);
        esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
        return PC_ERR_RETRY;
    }
    if (hend == NULL) {
        /*
         * 音频已经整段送到电脑了, 只是没等到响应头。
         * 这种情况绝对不能重发 —— 电脑很可能已经执行了(微信已经打开),
         * 重发就会再执行一次。交回去让上层只提示「电脑没回话」。
         */
        ESP_LOGE(TAG, "没等到电脑回话 (%d 字节, errno=%d)", rlen, errno);
        free(rbuf);
        close(s);
        esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
        return PC_ERR_GAVE_UP;
    }

    int status = -1;
    int body_at = (int)(hend - rbuf) + 4;
    *hend = '\0';                    /* 头当作字符串用 */
    if (sscanf(rbuf, "HTTP/%*d.%*d %d", &status) != 1) {
        status = -1;
    }
    user_out[0] = '\0';
    reply_out[0] = '\0';
    pc_hdr_find(rbuf, "X-User-Text", user_out, ucap);
    pc_hdr_find(rbuf, "X-Reply-Text", reply_out, rcap);

    char clen_txt[24];
    pc_hdr_find(rbuf, "Content-Length", clen_txt, sizeof(clen_txt));
    long long clen = clen_txt[0] ? atoll(clen_txt) : -1;
    ESP_LOGI(TAG, "电脑回 HTTP %d (音频 %lld 字节, 已到 %d)", status, clen,
             rlen - body_at);

    int16_t *pcm = heap_caps_malloc(4096, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pcm == NULL) {
        free(rbuf);
        close(s);
        esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
        return status;
    }

    int fill = 0;
    long long done = 0;
    int total = 0;
    if (status == 200 && rlen > body_at) {
        fill = rlen - body_at;
        if (fill > 4096) {
            fill = 4096;
        }
        memcpy((uint8_t *)pcm, rbuf + body_at, fill);
    }
    free(rbuf);

    if (status == 200) {
        audio_board_set_downlink_active(true);
        while (1) {
            if (fill >= 2) {
                int play = fill & ~1;
                audio_board_play_mono(pcm, play / 2);
                total += play;
                done += play;
                int rem = fill - play;
                if (rem) {
                    ((uint8_t *)pcm)[0] = ((uint8_t *)pcm)[play];
                }
                fill = rem;
            }
            if (clen > 0 && done >= clen) {
                break;
            }
            int n = recv(s, (uint8_t *)pcm + fill, 4096 - fill, 0);
            if (n <= 0) {
                break;
            }
            fill += n;
        }
        audio_board_set_downlink_active(false);
        ESP_LOGI(TAG, "回话播完 (%d 字节 PCM16)", total);
        if (total == 0) {
            /* 电脑只回文字、没给音频: 用一声「叮咚」兜底, 文字已经在屏上 */
            pc_local_beep(true);
        }
    }

    free(pcm);
    close(s);
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);   /* 用完把省电还回去 */
    return status;
}

/*
 * 上传前降到 8kHz。
 * A-law 这里是 16kHz 采样、每个样本 1 字节, 所以两两合并就得到 8kHz, 长度减半。
 *
 * 【为什么要两两取平均, 而不是直接取偶数位】
 * 直接抽取等于没做抗混叠: 4~8kHz 的成分会折回 0~4kHz。实测把「打开微信」
 * 听成 "I wish you", 识别直接崩。先解码成 PCM16 再取平均, 相当于一个
 * 简单低通, 音质明显干净, 识别率恢复。
 */
static int pc_downsample_8k(uint8_t *buf, int len)
{
    int j = 0;
    for (int i = 0; i + 1 < len; i += 2) {
        int32_t a = g711_alaw_decode(buf[i]);
        int32_t b = g711_alaw_decode(buf[i + 1]);
        buf[j++] = g711_alaw_encode((int16_t)((a + b) / 2));
    }
    return j;
}

/* ---------------- 一轮 ---------------- */
static void pc_do_turn(int64_t start_pos)
{
    char host[64], tok[96], line[160];
    pc_load_config(host, sizeof(host), tok, sizeof(tok));

    if (!tok[0]) {
        pc_show("小电脑还没配口令", 4000);
        pc_local_beep(false);
        return;
    }

    const int max_alaw = PC_MAX_SEC * PC_SAMPLE_RATE;
    uint8_t *alaw = heap_caps_malloc(max_alaw, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (alaw == NULL) {
        pc_show("内存不够, 这轮跳过", 4000);
        pc_local_beep(false);
        return;
    }

    int reader = audio_board_reader_open();
    if (reader < 0) {
        ESP_LOGE(TAG, "申请独立读游标失败");
        free(alaw);
        pc_show("麦克风忙, 再喊一次", 4000);
        pc_local_beep(false);
        return;
    }

    pc_show("小电脑: 听着呢", 3000);
    int len = pc_capture(alaw, max_alaw, reader, start_pos);
    audio_board_reader_close(reader);

    if (len <= 0) {
        free(alaw);
        pc_show("没听清, 再说一遍", 3000);
        pc_local_beep(false);
        return;
    }

    /* 16k -> 8k, 上传量直接砍一半 (见 pc_downsample_8k 的注释) */
    int len16 = len;
    len = pc_downsample_8k(alaw, len);
    /*
     * 【硬上限】这段链路能一次推完的上限就是 TCP 发送缓冲 16384 字节。
     * 超过它 send() 就会阻塞等 ACK, 而这条 Wi-Fi 的 ACK 经常几十秒不回来,
     * 表现就是"卡在 16384 不动"。话太长就截掉尾巴, 总比整句发不出去强。
     */
    if (len > PC_UPLINK_MAX) {
        ESP_LOGW(TAG, "话太长 (%d 字节), 截到 %d", len, PC_UPLINK_MAX);
        len = PC_UPLINK_MAX;
    }
    ESP_LOGI(TAG, "降到 8kHz: %d -> %d 字节", len16, len);

    /*
     * 【放静态区, 不放栈上】
     *
     * 原来是 char user_buf[128], reply_buf[192] —— 太小了:
     * 电脑把回话放在 X-Reply-Text 头里, 中文要 URL 编码, 一个汉字占 9 字节,
     * 192 字节只放得下 21 个字。长句被从中间切断, 还常切在汉字的 3 个字节
     * 中间, 解出来就是半个字 —— 屏幕上那句「半截话 + 方块」就是这么来的。
     *
     * 现在给到 512 / 1536 (约 170 个汉字), 一般回话都装得下。
     * 放 static 是为了不吃任务栈 (pc_task 只有 8KB);
     * 小电脑模块是串行的, 一轮只跑一次, 不存在并发访问。
     */
    static char user_buf[PC_TEXT_BUF];
    static char reply_buf[PC_REPLY_BUF];
    user_buf[0] = '\0';
    reply_buf[0] = '\0';
    /*
     * 这条链路慢但能跑通: 33KB 实测 ~12 秒发完, 电脑处理 ~20 秒。
     * 只有"连接/发送阶段"失败才重试(那时电脑肯定没收到);
     * 已经发完只是没等到回话的, 一律不重试。
     */
    int status = PC_ERR_RETRY;
    for (int attempt = 0; attempt < 2; attempt++) {
        if (attempt > 0) {
            ESP_LOGW(TAG, "第 %d 次重试上传", attempt + 1);
            vTaskDelay(pdMS_TO_TICKS(1500));
        }
        status = pc_http_turn(host, tok, alaw, len, user_buf, sizeof(user_buf),
                              reply_buf, sizeof(reply_buf));
        /* 已经整段送到电脑了就别重发 —— 电脑可能已经执行, 重发 = 重复执行 */
        if (status > 0 || status == PC_ERR_GAVE_UP) {
            break;
        }
    }
    free(alaw);

    /*
     * 屏幕只留最重要的那条 —— lcd_prompt_show() 是覆盖式的,
     * 连着调两次只会看到第二条。识别文本照样打进串口日志备查。
     */
    if (user_buf[0]) {
        ESP_LOGI(TAG, "听到: %s", user_buf);
    }
    if (status <= 0) {
        /* PC_ERR_GAVE_UP: 电脑收到了, 只是回话慢/没回来 —— 别谎报"没连上" */
        pc_show(status == PC_ERR_GAVE_UP ? "电脑收到了, 没回话" : "电脑没连上",
                status == PC_ERR_GAVE_UP ? 6000 : 4000);
        pc_local_beep(false);
        return;
    }
    if (reply_buf[0]) {
        /*
         * 不加「电脑: 」前缀 —— 屏幕一行只放得下 17 个汉字, 前缀白白吃掉 4 个字。
         * 横幅出现在「小电脑」这轮对话里, 本身就说明是电脑在回话。
         */
        pc_show(reply_buf, 6000);
    } else if (user_buf[0]) {
        snprintf(line, sizeof(line), "听到: %s", user_buf);
        pc_show(line, 4000);
    }

    if (status != 200) {
        snprintf(line, sizeof(line), "电脑没理我 (%d)", status);
        pc_show(line, 5000);
    }
}

static void pc_task(void *arg)
{
    (void)arg;
    while (1) {
        if (xSemaphoreTake(s_wake_sig, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        pc_do_turn(s_next_start);
        s_running = false;
        xSemaphoreGive(s_busy);
    }
}

/* ---------------- 对外接口 ---------------- */

static void pc_ensure_init(void)
{
    if (s_busy != NULL) {
        return;
    }
    /*
     * 【必须是二值信号量, 不能是互斥量】
     * 这一轮是「唤醒任务 take、工作线程 give」——跨任务交接。
     * 互斥量只能由持有者自己释放, 跨任务 give 会触发
     * assert failed: xTaskPriorityDisinherit -> 板子重启
     * (2026-11-08 实测: 失败路径上必重启)。
     * 二值信号量 give 一次 = 立即可用, 语义才对。
     */
    SemaphoreHandle_t made = xSemaphoreCreateBinary();
    if (made != NULL) {
        xSemaphoreGive(made);           /* 初始状态 = 空闲 */
    }
    portENTER_CRITICAL(&s_init_lock);
    if (s_busy == NULL) {
        s_busy = made;
        made = NULL;
    }
    portEXIT_CRITICAL(&s_init_lock);
    if (made != NULL) {
        vSemaphoreDelete(made);
    }
}

bool pc_voice_is_busy(void)
{
    return s_running;
}

/*
 * 常驻工作线程: 开机就建好, 之后每轮只发信号。
 * 为什么不在唤醒时现建 —— 运行期 xTaskCreate 需要一段连续内部 RAM,
 * 摄像头/TLS 跑过之后内部堆碎片化, 8KB 的栈也建不出来
 * (日志 "pc_voice: 任务创建失败", 表现就是喊小电脑完全没反应)。
 */
esp_err_t pc_voice_prepare(void)
{
    pc_ensure_init();
    if (s_busy == NULL) {
        return ESP_ERR_NO_MEM;
    }
    if (s_worker != NULL) {
        return ESP_OK;
    }
    if (s_wake_sig == NULL) {
        s_wake_sig = xSemaphoreCreateBinary();
        if (s_wake_sig == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (xTaskCreate(pc_task, "pc_voice", PC_TASK_STACK, NULL, 4, &s_worker) != pdPASS) {
        s_worker = NULL;
        ESP_LOGE(TAG, "工作线程创建失败: 栈=%d 内部空闲=%u 最大块=%u",
                 PC_TASK_STACK,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "工作线程已就绪 (栈 %d)", PC_TASK_STACK);
    return ESP_OK;
}

esp_err_t pc_voice_start_turn_at(int64_t start_pos)
{
    pc_ensure_init();
    if (s_busy == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /* 一个麦克风一个喇叭, 老人正在对讲就让路 —— 看护优先 */
    if (ai_client_get_state() != AI_STATE_IDLE ||
        audio_board_is_downlink_active()) {
        ESP_LOGW(TAG, "看护对讲正在用麦克风, 这一轮让路");
        return ESP_ERR_INVALID_STATE;
    }
    if (xSemaphoreTake(s_busy, 0) != pdTRUE) {
        ESP_LOGW(TAG, "上一轮还没结束, 忽略这次唤醒");
        return ESP_ERR_INVALID_STATE;
    }

    s_next_start = start_pos;
    s_running = true;
    if (s_worker == NULL && pc_voice_prepare() != ESP_OK) {
        s_running = false;
        xSemaphoreGive(s_busy);
        return ESP_FAIL;
    }
    xSemaphoreGive(s_wake_sig);
    return ESP_OK;
}
