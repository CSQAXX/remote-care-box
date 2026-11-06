/*
 * 远程看护盒子 - 板载音频 (ES8311 codec + ES7210 麦克风 ADC)
 *
 * 见 audio_board.h 里的硬件实测记录。
 *
 * 用 esp_codec_dev 组件 (espressif/esp_codec_dev v1.6.2) 的统一接口,
 * 源码放在 firmware/components/esp_codec_dev/ (纯本地组件, 不参与
 * 组件管理器的依赖求解 —— 当前网络连不上 registry)。
 *
 * 数据通路:
 *   录音: ES7210 (4 麦克风 ADC, 这里用 MIC1) --I2S--> GPIO41 (DIN) --> ESP32
 *   播放: ESP32 --I2S--> GPIO40 (DOUT) --> ES8311 (DAC) --> 功放 --> 喇叭
 *   I2C 控制: 复用 I2C0 (SDA=3/SCL=2), 与摄像头 SCCB、AW9523B 共用
 */

#include <string.h>
#include <stdlib.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "driver/i2s_std.h"
#include "esp_log.h"
#include "esp_check.h"
#include "esp_timer.h"

#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "es8311_codec.h"
#include "es7210_adc.h"

#include "camera_pins.h"
#include "audio_board.h"

static const char *TAG = "audio";

/* ---- 运行时状态 ---- */
static esp_codec_dev_handle_t s_codec   = NULL;  /* ES8311 */
static esp_codec_dev_handle_t s_adc     = NULL;  /* ES7210 */
static i2s_chan_handle_t     s_i2s_rx  = NULL;
static i2s_chan_handle_t     s_i2s_tx  = NULL;
static bool                  s_ready   = false;
static int16_t              *s_pcm_buf = NULL;   /* 内部 RAM 音频缓冲 */
static int                   s_out_vol = 60;     /* 当前输出音量 0-100 */
static float                 s_in_gain = 12.0f;  /* 当前输入增益 dB */

/* 内部 RAM 里给音频分一块, 避免 DMA 走 PSRAM (PSRAM DMA 会被摄像头抢) */
#define AUDIO_PCM_BUF_SAMPLES  1024   /* 1024 帧 = 4KB(立体声16bit) */
#define AUDIO_PCM_BUF_BYTES    (AUDIO_PCM_BUF_SAMPLES * 2 * sizeof(int16_t))

/* ---- 后台任务参数 ---- */
#define AUDIO_MONITOR_PERIOD_MS   1000   /* 每秒打一条电平日志 */
#define AUDIO_TONE_PERIOD_MS      3000   /* 测试音播放间隔 */
#define AUDIO_TONE_DURATION_MS    1000   /* 每次测试音时长 */
#define AUDIO_SPEAK_RMS_THRESHOLD 100    /* RMS 超过这个值算"有人在说话" */
#define AUDIO_MONITOR_CHUNK       256    /* 每次读多少帧 */
#define AUDIO_CAPTURE_CHUNK       256    /* 采集任务每次读多少帧 */

/* ---- 后台任务句柄与统计 ---- */
static TaskHandle_t s_monitor_task = NULL;
static TaskHandle_t s_tone_task    = NULL;
static TaskHandle_t s_capture_task = NULL;
static volatile bool s_capture_running = false;
static volatile audio_board_stats_t s_stats = {0};
static volatile bool s_monitor_running = false;
static volatile bool s_tone_running    = false;

/* ---------------- 采集环形缓冲 (单读者架构) ----------------
 *
 * 为什么要有这个:
 *   I2S RX 只有一个消费者能安心用。之前 monitor task 和 HTTP /audio
 *   handler 各自调 audio_board_read(), 两边抢同一个 DMA 队列,
 *   结果是谁都不完整 —— 表现为"麦克风有时候没反应"。
 *
 * 现在改成: 一个采集任务独占 I2S RX, 读进来之后:
 *   - 双声道原样存进环形缓冲 (给下行/调试用)
 *   - 同时降混成单声道存进另一个环形缓冲 (给上行用)
 *   - 顺便算 peak/rms (监测任务只读统计, 不碰 I2S)
 *
 * 环形缓冲容量:
 *   16kHz 单声道 int16 = 32KB/s。存 1 秒 = 32KB。
 *
 * 【2026-11-03 修正】
 *   原本这块放内部 RAM, 但实测内部 RAM 只剩 28KB —— LCD 双缓冲
 *   (2 x 12.8KB)、音频 PCM 缓冲、各任务栈全占了内部 RAM,
 *   结果测试音/下行自环的缓冲全部分配失败。
 *
 *   环形缓冲挪到 PSRAM: 它只是 CPU 访问的普通内存, 不参与 DMA
 *   (I2S DMA 读的是 s_pcm_buf, 那块必须留内部 RAM)。
 *   读写指针用互斥锁保护, 没有 cache 一致性问题。
 *   -> 48KB 从内部 RAM 挪到 PSRAM, 内部 RAM 直接多出 48KB。
 */
#define AUDIO_RING_MONO_SAMPLES   8000    /* 500ms @16kHz 单声道 */
#define AUDIO_RING_STEREO_SAMPLES 8000    /* 500ms @16kHz 双声道 (帧) */

static int16_t *s_ring_mono   = NULL;   /* 单声道样本 (上行用) */
static int16_t *s_ring_stereo = NULL;   /* 双声道交错样本 */
static size_t   s_ring_mono_w = 0;      /* 写指针 */
static size_t   s_ring_mono_r = 0;      /* 读指针 */
static size_t   s_ring_st_w   = 0;
static size_t   s_ring_st_r   = 0;
static SemaphoreHandle_t s_ring_lock = NULL;   /* 保护上面的指针 */
static volatile bool s_downlink_active = false; /* 下行对讲中 -> 上行静音 */

/* 采集任务定义在文件后面, 这里先声明 */
static void audio_capture_task(void *arg);

/*
 * 环形缓冲: 写入 samples 个样本 (调用者持有 lock)
 * 满了就覆盖最旧的 (丢旧数据, 保最新 —— 对实时音频是对的取舍)
 */
static void ring_write_mono_locked(const int16_t *in, int samples)
{
    for (int i = 0; i < samples; i++) {
        s_ring_mono[s_ring_mono_w] = in[i];
        s_ring_mono_w = (s_ring_mono_w + 1) % AUDIO_RING_MONO_SAMPLES;
        if (s_ring_mono_w == s_ring_mono_r) {
            /* 写满了, 读指针跟着前移一格, 丢最老的一个样本 */
            s_ring_mono_r = (s_ring_mono_r + 1) % AUDIO_RING_MONO_SAMPLES;
        }
    }
}

static void ring_write_stereo_locked(const int16_t *in, int frames)
{
    for (int i = 0; i < frames; i++) {
        s_ring_stereo[s_ring_st_w]     = in[i * 2];      /* 左 */
        s_ring_stereo[s_ring_st_w + 1] = in[i * 2 + 1];  /* 右 */
        s_ring_st_w = (s_ring_st_w + 2) % AUDIO_RING_STEREO_SAMPLES;
        if (s_ring_st_w == s_ring_st_r) {
            s_ring_st_r = (s_ring_st_r + 2) % AUDIO_RING_STEREO_SAMPLES;
        }
    }
}

/* 读取单声道样本 (返回实际读到的数量) */
static int ring_read_mono_locked(int16_t *dest, int want)
{
    int avail = (int)((s_ring_mono_w + AUDIO_RING_MONO_SAMPLES - s_ring_mono_r)
                      % AUDIO_RING_MONO_SAMPLES);
    int n = (want < avail) ? want : avail;
    for (int i = 0; i < n; i++) {
        dest[i] = s_ring_mono[s_ring_mono_r];
        s_ring_mono_r = (s_ring_mono_r + 1) % AUDIO_RING_MONO_SAMPLES;
    }
    return n;
}

/* 读取双声道帧 (返回实际读到的帧数) */
static int ring_read_stereo_locked(int16_t *dest, int want_frames)
{
    int avail = (int)((s_ring_st_w + AUDIO_RING_STEREO_SAMPLES - s_ring_st_r)
                      % AUDIO_RING_STEREO_SAMPLES) / 2;
    int n = (want_frames < avail) ? want_frames : avail;
    for (int i = 0; i < n; i++) {
        dest[i * 2]     = s_ring_stereo[s_ring_st_r];
        dest[i * 2 + 1] = s_ring_stereo[s_ring_st_r + 1];
        s_ring_st_r = (s_ring_st_r + 2) % AUDIO_RING_STEREO_SAMPLES;
    }
    return n;
}

esp_err_t audio_board_init(void)
{
    if (s_ready) {
        return ESP_OK;
    }

    ESP_LOGI(TAG, "==== 音频初始化 (ES8311 + ES7210) ====");

    /* ---------- 1. 建 I2S 通道 ---------- */
    /*
     * esp_codec_dev 在 IDF 5.x 下不自己配 I2S 引脚, 要应用先建好通道
     * 把 handle 传进去。引脚是 BOX3 官方 board config 的:
     *   MCLK=21 WS=39 BCLK=38 DIN=41 DOUT=40
     */
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num   = 6;
    chan_cfg.dma_frame_num  = 320;
    chan_cfg.auto_clear_after_cb = true;
    chan_cfg.auto_clear_before_cb = false;
    chan_cfg.intr_priority  = 5;   /* 音频比摄像头任务(6)低, 但比 LCD(2)高 */

    esp_err_t err = i2s_new_channel(&chan_cfg, &s_i2s_tx, &s_i2s_rx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_new_channel 失败: %s", esp_err_to_name(err));
        return err;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg = {
            .sample_rate_hz  = AUDIO_SAMPLE_RATE,
            .clk_src         = I2S_CLK_SRC_DEFAULT,
            .mclk_multiple   = I2S_MCLK_MULTIPLE_256,
        },
        .slot_cfg = {
            .data_bit_width  = I2S_DATA_BIT_WIDTH_16BIT,
            .slot_bit_width  = I2S_SLOT_BIT_WIDTH_AUTO,
            .slot_mode       = I2S_SLOT_MODE_STEREO,
            .slot_mask       = I2S_STD_SLOT_BOTH,
            .ws_width        = I2S_DATA_BIT_WIDTH_16BIT,
            .ws_pol          = false,
            .bit_shift       = true,
        },
        .gpio_cfg = {
            .mclk    = AUDIO_I2S_MCLK_PIN,
            .bclk    = AUDIO_I2S_BCLK_PIN,
            .ws      = AUDIO_I2S_WS_PIN,
            .dout    = AUDIO_I2S_DOUT_PIN,
            .din     = AUDIO_I2S_DIN_PIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv   = false,
            },
        },
    };

    err = i2s_channel_init_std_mode(s_i2s_tx, &std_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_channel_init_std_mode(TX) 失败: %s", esp_err_to_name(err));
        goto fail;
    }
    err = i2s_channel_init_std_mode(s_i2s_rx, &std_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_channel_init_std_mode(RX) 失败: %s", esp_err_to_name(err));
        goto fail;
    }
    ESP_LOGI(TAG, "I2S 通道就绪 (MCLK=%d WS=%d BCLK=%d DIN=%d DOUT=%d, %dHz)",
             AUDIO_I2S_MCLK_PIN, AUDIO_I2S_WS_PIN, AUDIO_I2S_BCLK_PIN,
             AUDIO_I2S_DIN_PIN, AUDIO_I2S_DOUT_PIN, AUDIO_SAMPLE_RATE);

    /* ---------- 2. I2C 控制接口 (复用已装好的 I2C0) ---------- */
    audio_codec_i2c_cfg_t i2c_cfg = {
        .port           = BOARD_I2C_PORT,
        .addr           = 0,       /* 下面按 codec 分别建 ctrl_if */
        .clock_speed_hz = BOARD_I2C_FREQ_HZ,
    };

    /* ---------- 3. I2S 数据通路 (ES8311 和 ES7210 共用一组通道) ---------- */
    audio_codec_i2s_cfg_t i2s_data_cfg = {
        .port      = I2S_NUM_0,
        .rx_handle = s_i2s_rx,
        .tx_handle = s_i2s_tx,
        .clk_src   = I2S_CLK_SRC_DEFAULT,
    };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_data_cfg);
    if (data_if == NULL) {
        ESP_LOGE(TAG, "audio_codec_new_i2s_data 失败");
        goto fail;
    }

    /* ---------- 4. ES8311 (播放 DAC) ---------- */
    {
        audio_codec_i2c_cfg_t c = i2c_cfg;
        c.addr = ES8311_I2C_ADDR;
        const audio_codec_ctrl_if_t *ctrl = audio_codec_new_i2c_ctrl(&c);
        if (ctrl == NULL) {
            ESP_LOGE(TAG, "audio_codec_new_i2c_ctrl(ES8311) 失败");
            goto fail;
        }

        es8311_codec_cfg_t es8311_cfg = {
            .ctrl_if      = ctrl,
            .gpio_if      = audio_codec_new_gpio(),
            .codec_mode   = ESP_CODEC_DEV_TYPE_OUT,
            .pa_pin       = -1,          /* 功放由 AW9523B XIO5 控制, 不走 codec GPIO */
            .pa_reverted  = false,
            .master_mode  = false,       /* ESP32 做 I2S master */
            .use_mclk     = true,
            .digital_mic  = false,
            .invert_mclk  = false,
            .invert_sclk  = false,
            .hw_gain      = {
                .pa_voltage        = 3.3f,
                .codec_dac_voltage = 3.3f,
                .pa_gain           = 0.0f,
            },
            .no_dac_ref   = true,
            .mclk_div     = 256,
        };

        esp_codec_dev_cfg_t codec_cfg = {
            .dev_type  = ESP_CODEC_DEV_TYPE_OUT,
            .codec_if  = es8311_codec_new(&es8311_cfg),
            .data_if   = data_if,
        };
        if (codec_cfg.codec_if == NULL) {
            ESP_LOGE(TAG, "es8311_codec_new 失败");
            goto fail;
        }
        s_codec = esp_codec_dev_new(&codec_cfg);
        if (s_codec == NULL) {
            ESP_LOGE(TAG, "esp_codec_dev_new(ES8311) 失败");
            goto fail;
        }
    }

    /* ---------- 4. ES7210 (麦克风 ADC) ---------- */
    {
        audio_codec_i2c_cfg_t c = i2c_cfg;
        c.addr = ES7210_I2C_ADDR;
        const audio_codec_ctrl_if_t *ctrl = audio_codec_new_i2c_ctrl(&c);
        if (ctrl == NULL) {
            ESP_LOGE(TAG, "audio_codec_new_i2c_ctrl(ES7210) 失败");
            goto fail;
        }

        es7210_codec_cfg_t es7210_cfg = {
            .ctrl_if      = ctrl,
            .master_mode  = false,
            /*
             * BOX3 上是 ES7210 双麦克风 (差分对):
             *   MIC1 (LIN/RIN) 和 MIC2 (LIP/RIP) 组成立体声。
             * 只开 MIC1 的话另一路是静音, 录出来是单声道。
             */
            .mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2,
            .mclk_src     = ES7210_MCLK_FROM_PAD,
            .mclk_div     = 256,
        };

        esp_codec_dev_cfg_t adc_cfg = {
            .dev_type  = ESP_CODEC_DEV_TYPE_IN,
            .codec_if  = es7210_codec_new(&es7210_cfg),
            .data_if   = data_if,
        };
        if (adc_cfg.codec_if == NULL) {
            ESP_LOGE(TAG, "es7210_codec_new 失败");
            goto fail;
        }
        s_adc = esp_codec_dev_new(&adc_cfg);
        if (s_adc == NULL) {
            ESP_LOGE(TAG, "esp_codec_dev_new(ES7210) 失败");
            goto fail;
        }
    }

    /* ---------- 5. 打开 codec ---------- */
    /*
     * 顺序很重要: 必须【先开 ES7210(录音), 再开 ES8311(播放)】。
     *
     * 原因 (esp_codec_dev 的 i2s data_if 实现, audio_codec_data_i2s.c):
     * 全双工时 RX 和 TX 共享时钟, 单独改 RX 的时钟配置在 TX 已使能的情况下
     * 不生效, 驱动靠一段"反向同步"代码来补:
     *
     *   if (skip == false && playback == false &&
     *       paired && paired->out_handle != NULL && paired->out_enable == false) {
     *       // TX 是 master, 只设 RX 不生效, 需要把 TX 也 reconfig 一遍
     *       _i2s_drv_enable(paired, true, false);
     *       set_drv_fs(paired->out_handle, true, ...);
     *       _i2s_drv_enable(paired, true, true);
     *   }
     *
     * 注意那个 paired->out_enable == false 的前提 —— 只有当 TX 【还没开】时
     * 才会走这段同步逻辑。所以:
     *   先开 ES8311 再开 ES7210 -> RX 改时钟时 TX 已经 enable,
     *                               同步逻辑被跳过 -> TX 的 DMA 配置和
     *                               实际时钟对不上 -> 播放写入超时卡死
     *   先开 ES7210 再开 ES8311 -> 开 TX 时会走 set_fs 的配对分支,
     *                               格式对齐, 全双工正常
     *
     * 实际表现 (实测): 反序打开时 i2s_channel_write 返回 ESP_ERR_TIMEOUT
     * (要 1024 字节只写了 256), 喇叭完全没声音; 正序打开两者都正常。
     */
    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16,
        .channel         = 2,
        .channel_mask    = 0,
        .sample_rate     = AUDIO_SAMPLE_RATE,
        .mclk_multiple   = I2S_MCLK_MULTIPLE_256,
    };

    /* 先开录音 */
    if (esp_codec_dev_open(s_adc, &fs) != 0) {
        ESP_LOGE(TAG, "esp_codec_dev_open(ES7210) 失败");
        goto fail;
    }
    /* 再开播放 */
    if (esp_codec_dev_open(s_codec, &fs) != 0) {
        ESP_LOGE(TAG, "esp_codec_dev_open(ES8311) 失败");
        goto fail;
    }

    /* ---------- 6. 内部 RAM PCM 缓冲 ---------- */
    s_pcm_buf = heap_caps_malloc(AUDIO_PCM_BUF_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (s_pcm_buf == NULL) {
        ESP_LOGE(TAG, "PCM 缓冲分配失败 (%d 字节内部RAM)", AUDIO_PCM_BUF_BYTES);
        goto fail;
    }

    /* ---------- 7. 音量 ---------- */
    /*
     * 默认值:
     *   输出音量 60/100 —— 够用但不太吵
     *   输入增益 30 dB  —— 【2026-11-03 改】
     *     之前是 12dB, 但那时候增益命令名和摄像头冲突, 实际根本没设上,
     *     12dB 这个数是照着"没生效"的状态定的。
     *     现在实测 30dB 时 L 声道 peak=519 (有效信号), 30dB 是合适的起点。
     *     ES7210 的 MIC1 是差分输入, 30dB 正好是驱动 open() 时的默认值。
     */
    s_out_vol = 60;
    s_in_gain = 30.0f;
    esp_codec_dev_set_out_vol(s_codec, s_out_vol);
    esp_codec_dev_set_in_gain(s_adc, s_in_gain);

    s_ready = true;
    ESP_LOGI(TAG, "音频就绪: ES8311(0x%02X) 播放 + ES7210(0x%02X) 录音, %dHz",
             ES8311_I2C_ADDR, ES7210_I2C_ADDR, AUDIO_SAMPLE_RATE);

    /* ---------- 8. 采集环形缓冲 + 互斥锁 ---------- */
    s_ring_lock = xSemaphoreCreateMutex();
    s_ring_mono   = heap_caps_malloc(AUDIO_RING_MONO_SAMPLES * sizeof(int16_t),
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_ring_stereo = heap_caps_malloc(AUDIO_RING_STEREO_SAMPLES * sizeof(int16_t),
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_ring_lock == NULL || s_ring_mono == NULL || s_ring_stereo == NULL) {
        ESP_LOGE(TAG, "环形缓冲分配失败 (需要 %d 字节 PSRAM)",
                 (AUDIO_RING_MONO_SAMPLES + AUDIO_RING_STEREO_SAMPLES) * (int)sizeof(int16_t));
        goto fail;
    }
    s_ring_mono_w = s_ring_mono_r = 0;
    s_ring_st_w   = s_ring_st_r   = 0;

    /*
     * 采集任务是 I2S RX 的唯一读者, 必须在 init 末尾就起来。
     * 为什么放这里而不是 start_monitor():
     *   开机自检 (audio_board_selftest) 也要读录音数据, 而它读的是
     *   环形缓冲 —— 采集任务不启动就永远读不到东西。
     */
    s_capture_running = true;
    /*
     * 【2026-11-03】钉到 core 0。
     * 环形缓冲挪到 PSRAM 之后, 写指针在采集任务, 读指针在 httpd 任务。
     * ESP32-S3 的 PSRAM 缓存是每核独立的, 两个任务跑在不同核上会各自
     * 看到不同的缓存内容 -> 读到陈旧数据 / 撕裂。
     * 全部钉到 APP_CORE_ID (core 0) 就没这个问题, 和视频那条链路一致。
     */
    if (xTaskCreatePinnedToCore(audio_capture_task, "audio_cap", 4096, NULL, 5,
                                 &s_capture_task, APP_CORE_ID) != pdPASS) {
        ESP_LOGE(TAG, "采集任务创建失败");
        s_capture_running = false;
        goto fail;
    }

    /*
     * 等 codec 内部软复位完成。ES8311/ES7210 的 open 里有一串寄存器写入,
     * 芯片要几百 ms 才稳定, 立刻读 I2S 会拿到空数据/失败。
     */
    vTaskDelay(pdMS_TO_TICKS(300));
    return ESP_OK;

fail:
    audio_board_deinit();
    return ESP_FAIL;
}

void audio_board_deinit(void)
{
    s_capture_running = false;
    s_monitor_running = false;
    s_tone_running    = false;

    /* 给任务一点时间自己退出, 再回收资源 */
    for (int i = 0; i < 20 && (s_capture_task || s_monitor_task || s_tone_task); i++) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }

    if (s_adc) {
        esp_codec_dev_close(s_adc);
        esp_codec_dev_delete(s_adc);
        s_adc = NULL;
    }
    if (s_codec) {
        esp_codec_dev_close(s_codec);
        esp_codec_dev_delete(s_codec);
        s_codec = NULL;
    }
    if (s_i2s_tx) { i2s_del_channel(s_i2s_tx); s_i2s_tx = NULL; }
    if (s_i2s_rx) { i2s_del_channel(s_i2s_rx); s_i2s_rx = NULL; }
    if (s_pcm_buf) { free(s_pcm_buf); s_pcm_buf = NULL; }
    if (s_ring_mono)   { free(s_ring_mono);   s_ring_mono   = NULL; }
    if (s_ring_stereo) { free(s_ring_stereo); s_ring_stereo = NULL; }
    if (s_ring_lock)   { vSemaphoreDelete(s_ring_lock); s_ring_lock = NULL; }
    s_ready = false;
}

int audio_board_read(int16_t *dest, int sample_count)
{
    if (!s_ready || !s_adc || dest == NULL || sample_count <= 0) {
        return -1;
    }
    /*
     * I2S RX 现在由采集任务独占, 外部不能直接读。
     * 统一走环形缓冲。
     */
    if (s_ring_lock == NULL) {
        return -1;
    }
    xSemaphoreTake(s_ring_lock, portMAX_DELAY);
    int n = ring_read_stereo_locked(dest, sample_count);
    xSemaphoreGive(s_ring_lock);
    return n;
}

int audio_board_read_mono(int16_t *dest, int sample_count)
{
    if (!s_ready || dest == NULL || sample_count <= 0) {
        return -1;
    }
    if (s_ring_lock == NULL) {
        return -1;
    }
    xSemaphoreTake(s_ring_lock, portMAX_DELAY);
    int n = ring_read_mono_locked(dest, sample_count);
    xSemaphoreGive(s_ring_lock);
    return n;
}

bool audio_board_is_downlink_active(void)
{
    return s_downlink_active;
}

void audio_board_set_downlink_active(bool active)
{
    s_downlink_active = active;
    ESP_LOGI(TAG, "下行对讲 %s (上行%s)",
             active ? "开始" : "结束",
             active ? "静音以避免啸叫" : "恢复");
}

int audio_board_write(const int16_t *src, int sample_count)
{
    if (!s_ready || !s_codec || src == NULL || sample_count <= 0) {
        return -1;
    }
    size_t bytes = (size_t)sample_count * 2 * sizeof(int16_t);
    size_t written = 0;
    int ret = i2s_channel_write(s_i2s_tx, src, bytes, &written, pdMS_TO_TICKS(200));
    if (ret != ESP_OK) {
        /* 记下真实错误码, 否则只能看到"失败"两个字, 没法定位 */
        static int last_err = 0;
        if (ret != last_err) {
            last_err = ret;
            ESP_LOGE(TAG, "I2S 写入失败: %s (要 %u 字节, 写了 %u)",
                     esp_err_to_name(ret), (unsigned)bytes, (unsigned)written);
        }
        return -1;
    }
    return (int)(written / (2 * sizeof(int16_t)));
}

/*
 * 播放单声道 PCM (16kHz) —— 自动复制成双声道送进 I2S。
 *
 * 下行链路用: 需求文档 3.6 规定下行是 PCM16 16kHz 单声道 20ms 一包。
 * ES8311 走的是双声道 I2S, 所以这里做一次立体声展开。
 */
int audio_board_play_mono(const int16_t *src, int sample_count)
{
    if (!s_ready || s_codec == NULL || src == NULL || sample_count <= 0) {
        return -1;
    }
    if (sample_count > 2048) sample_count = 2048;

    /*
     * 【2026-11-03 修正】
     * 原来这里是 int16_t stereo[2048 * 2]; —— 8KB 栈上数组!
     * 这函数在 httpd 的 /audio/down handler 里被调用, 而 httpd 任务栈
     * 只有 10240 字节, 8KB 的局部数组 + httpd 自己的开销很容易爆栈
     * (表现就是下行一按就重启)。改成堆上分配。
     */
    int16_t *stereo = heap_caps_malloc((size_t)sample_count * 2 * sizeof(int16_t),
                                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (stereo == NULL) {
        return -1;
    }
    for (int i = 0; i < sample_count; i++) {
        stereo[i * 2]     = src[i];
        stereo[i * 2 + 1] = src[i];
    }
    int ret = audio_board_write(stereo, sample_count);
    free(stereo);
    return ret;
}

int audio_board_set_input_gain(float db)
{
    if (!s_ready) return -1;
    int ret = esp_codec_dev_set_in_gain(s_adc, db);
    if (ret == 0) {
        s_in_gain = db;
    }
    return ret;
}

int audio_board_set_output_volume(int volume)
{
    if (!s_ready) return -1;
    if (volume < 0) volume = 0;
    if (volume > 100) volume = 100;
    int ret = esp_codec_dev_set_out_vol(s_codec, volume);
    if (ret == 0) {
        s_out_vol = volume;
    }
    return ret;
}

int audio_board_get_output_volume(void)
{
    return s_ready ? s_out_vol : -1;
}

float audio_board_get_input_gain(void)
{
    return s_ready ? s_in_gain : 0.0f;
}

int audio_board_set_mute(bool mute)
{
    if (!s_ready) return -1;
    return esp_codec_dev_set_out_mute(s_codec, mute);
}

int audio_board_read_reg(int reg, int *val)
{
    if (!s_ready) return -1;
    return esp_codec_dev_read_reg(s_codec, reg, val);
}

/*
 * 【2026-11-03 新增】读取 ES7210 (麦克风 ADC) 的关键寄存器。
 *
 * 排查麦克风电平问题时需要确认:
 *   0x43/0x44  MIC1/MIC2 增益 (低 4 位是增益档位, bit4 是 PGA 使能)
 *   0x41/0x42  MIC12/MIC34 偏置电压 (0x70 = 2.87V)
 *   0x47-0x4A  MIC1-4 模拟电源
 *   0x4B/0x4C  MIC12/MIC34 偏置+ADC+PGA 电源
 *   0x40       模拟电源控制
 *   0x14/0x15  数字静音
 *
 * 返回 0 表示成功。
 */
int audio_board_read_adc_reg(int reg, int *val)
{
    if (!s_ready || s_adc == NULL) return -1;
    return esp_codec_dev_read_reg(s_adc, reg, val);
}

/*
 * 一次性把 ES7210 关键寄存器全读出来, 方便看 mic 链路到底哪一环不对。
 */
void audio_board_dump_adc_regs(void)
{
    if (!s_ready || s_adc == NULL) {
        ESP_LOGE(TAG, "音频没就绪, 无法读 ES7210");
        return;
    }
    static const char *names[] = {
        "0x40 模拟电源    ", "0x41 MIC12 偏置   ", "0x42 MIC34 偏置   ",
        "0x43 MIC1 增益   ", "0x44 MIC2 增益   ", "0x45 MIC3 增益   ",
        "0x46 MIC4 增益   ", "0x47 MIC1 电源   ", "0x48 MIC2 电源   ",
        "0x49 MIC3 电源   ", "0x4A MIC4 电源   ", "0x4B MIC12 电源  ",
        "0x4C MIC34 电源  ", "0x00 复位        ", "0x01 时钟        ",
        "0x06 掉电        ", "0x14 数字静音    ", "0x15 数字静音    ",
    };
    static const int regs[] = {
        0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48,
        0x49, 0x4A, 0x4B, 0x4C, 0x00, 0x01, 0x06, 0x14, 0x15,
    };

    ESP_LOGI(TAG, "---- ES7210 寄存器 ----");
    for (int i = 0; i < (int)(sizeof(regs) / sizeof(regs[0])); i++) {
        int v = -1;
        if (esp_codec_dev_read_reg(s_adc, regs[i], &v) == 0) {
            ESP_LOGI(TAG, "  %s = 0x%02x", names[i], v);
        } else {
            ESP_LOGI(TAG, "  %s = 读失败", names[i]);
        }
    }
    ESP_LOGI(TAG, "  当前输入增益设置 = %.1f dB", s_in_gain);
}

bool audio_board_is_ready(void)
{
    return s_ready;
}

/* ---------------- 音频自检 ----------------
 *
 * 目的: 光看 "Open codec device OK" 不能证明真的有声音。
 * 这里做两件事:
 *   1. 播放: 生成一段 440Hz 正弦波推到 DAC, 听喇叭有没有声音
 *      (也顺便验证 I2S TX 通路)
 *   2. 录音: 从 ADC 读一段, 统计峰值/RMS —— 说话时电平应该明显跳动,
 *      静音时应该接近 0
 *
 * 返回值: 录音峰值 (0-32767), -1 表示读失败
 *
 * 关键: 帧数 -> 字节数 的换算。
 *   sample_count 是"帧数"(左右声道算一帧), 立体声 16bit 每帧 4 字节。
 *   之前按 sample_count*2 字节分配缓冲, 实际读写 sample_count*4 字节,
 *   溢出一半直接踩坏内部堆 -> 后来 lcd 任务 malloc 时 heap 崩溃。
 *   这里统一用 AUDIO_FRAME_BYTES 换算。
 *
 * 所有 PCM 缓冲都放堆上。app_main 的任务栈只有 3584 字节(已调到 8192),
 * 栈上放 1600 帧 int16 会爆栈。
 */

/* 立体声 16bit: 每帧 = 2 声道 x 2 字节 = 4 字节 */
#define AUDIO_FRAME_BYTES     (2 * sizeof(int16_t))

int audio_board_selftest(int duration_ms, int16_t *out_buf, int out_cap_samples)
{
    if (!s_ready) {
        return -1;
    }

    /* ---- 1. 播放 440Hz 正弦波 ---- */
    {
        const int samples = 1600;                 /* 100ms @16kHz */
        int16_t *tone = heap_caps_malloc(samples * AUDIO_FRAME_BYTES,
                                         MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        if (tone == NULL) {
            ESP_LOGE(TAG, "自检: 测试音缓冲分配失败");
            return -1;
        }
        /* 用 double 算相位, 避免整数除法累积误差 */
        for (int i = 0; i < samples; i++) {
            double phase = 2.0 * 3.14159265358979 * 440.0 * i / AUDIO_SAMPLE_RATE;
            int16_t v = (int16_t)(sin(phase) * 12000.0);   /* 幅度约 36% 满量程 */
            tone[i * 2]     = v;      /* 左 */
            tone[i * 2 + 1] = v;      /* 右 */
        }
        int written = audio_board_write(tone, samples);
        ESP_LOGI(TAG, "自检: 播放 440Hz 测试音, 写入 %d/%d 帧", written, samples);
        free(tone);
    }

    /* ---- 2. 录音并统计电平 ---- */
    int peak = 0;
    long long sum_sq = 0;
    int total = 0;
    int elapsed = 0;
    int retry = 0;
    const int chunk = 256;                       /* 每次读 256 帧 */
    int16_t *buf = heap_caps_malloc(chunk * AUDIO_FRAME_BYTES,
                                    MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (buf == NULL) {
        ESP_LOGE(TAG, "自检: 录音缓冲分配失败");
        return -1;
    }

    while (elapsed < duration_ms) {
        int n = audio_board_read(buf, chunk);
        if (n <= 0) {
            /*
             * 第一次读可能失败: I2S RX 刚 enable, DMA 缓冲还没填上,
             * 或 ES7210 还在软复位。给点时间重试几次。
             */
            if (retry++ < 20) {
                vTaskDelay(pdMS_TO_TICKS(50));
                continue;
            }
            ESP_LOGW(TAG, "自检: 录音读取失败 (重试 %d 次)", retry);
            free(buf);
            return -1;
        }
        retry = 0;
        for (int i = 0; i < n * 2; i++) {          /* 交错立体声, 遍历全部样本 */
            int v = buf[i];
            if (v < 0) v = -v;
            if (v > peak) peak = v;
            sum_sq += (long long)v * v;
        }
        /* 把第一段拷给调用者看波形 (单位: int16 样本数) */
        if (out_buf != NULL && out_cap_samples > 0 && total == 0) {
            int copy = n * 2;
            if (copy > out_cap_samples) copy = out_cap_samples;
            memcpy(out_buf, buf, copy * sizeof(int16_t));
        }
        total += n * 2;
        elapsed += (n * 1000) / AUDIO_SAMPLE_RATE;
    }
    free(buf);

    int rms = (total > 0) ? (int)(sqrt((double)sum_sq / total)) : 0;
    ESP_LOGI(TAG, "自检: 录音 %d 个样本, 峰值=%d, RMS=%d", total, peak, rms);
    if (peak < 100) {
        ESP_LOGW(TAG, "自检: 录音电平极低 —— 可能是没说话, 或麦克风没接上");
    }
    return peak;
}

/* ---------------- 后台任务: 采集任务 (独占 I2S RX) ----------------
 *
 * 这是整个音频链路的唯一 I2S 读者。
 *
 * 它做三件事:
 *   1. 从 I2S RX 读双声道交错 PCM
 *   2. 原样存进双声道环形缓冲 + 降混成单声道存进单声道环形缓冲
 *   3. 顺便算 peak/rms (监测任务只读统计, 完全不碰 I2S)
 *
 * 为什么要单独一个任务:
 *   I2S RX 只有一个 DMA 队列, 多个消费者去 read 会互相偷帧。
 *   之前 monitor task 和 HTTP /audio handler 各自 read, 结果两边都拿到
 *   不完整的数据, 表现为"麦克风时好时坏"。现在单一读者 + 环形缓冲分发。
 *
 * 半双工对讲 (v1):
 *   下行播放期间 (s_downlink_active), 上行数据不再进环形缓冲 ——
 *   否则喇叭回声会被麦克风拾回去传上去, 变成对讲啸叫。
 */
static void audio_capture_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "音频采集任务已启动 (16kHz 立体声 -> 单声道上行)");

    int16_t *stereo = heap_caps_malloc(AUDIO_CAPTURE_CHUNK * AUDIO_FRAME_BYTES,
                                        MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    int16_t *mono   = heap_caps_malloc(AUDIO_CAPTURE_CHUNK * sizeof(int16_t),
                                        MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (stereo == NULL || mono == NULL) {
        ESP_LOGE(TAG, "采集任务: 缓冲分配失败");
        free(stereo);
        free(mono);
        vTaskDelete(NULL);
        return;
    }

    while (s_capture_running) {
        /* 直接读 I2S —— 这里是唯一的读者 */
        size_t bytes = (size_t)AUDIO_CAPTURE_CHUNK * AUDIO_FRAME_BYTES;
        size_t got = 0;
        esp_err_t ret = i2s_channel_read(s_i2s_rx, stereo, bytes, &got, pdMS_TO_TICKS(200));
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "采集: I2S 读取失败 (%s)", esp_err_to_name(ret));
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        if (got == 0) {
            continue;   /* 超时, 回去再读 */
        }

        int frames = (int)(got / AUDIO_FRAME_BYTES);
        int samples = frames * 2;   /* 交错立体声, 样本数 = 帧数 x 2 */

        /*
         * 左右声道分开统计。
         *
         * ES7210 上 MIC1/MIC2 是差分对, 两个麦克风极性可能是反的。
         * 如果 L/R 拾到同一信号但极性相反, 降混 (L+R)/2 会互相抵消 ——
         * 表现就是"左右单看有信号, 合起来只剩底噪"。
         * 分开统计才能定位这个问题。
         */
        int peak_l = 0, peak_r = 0;
        long long sum_sq_l = 0, sum_sq_r = 0;
        for (int i = 0; i < frames; i++) {
            int vl = stereo[i * 2];
            int vr = stereo[i * 2 + 1];
            if (vl < 0) vl = -vl;
            if (vr < 0) vr = -vr;
            if (vl > peak_l) peak_l = vl;
            if (vr > peak_r) peak_r = vr;
            sum_sq_l += (long long)stereo[i * 2] * stereo[i * 2];
            sum_sq_r += (long long)stereo[i * 2 + 1] * stereo[i * 2 + 1];
        }
        int rms_l = (int)(sqrt((double)sum_sq_l / frames));
        int rms_r = (int)(sqrt((double)sum_sq_r / frames));

        /*
         * 降混成单声道。
         *
         * 【2026-11-03 修正 —— 这是麦克风电平一直上不去的真正原因】
         *
         * 实测 (新增左右分声道统计):
         *     mgain=30dB -> L: peak=519  R: peak=5
         * 左右差 100 倍! 也就是说 BOX3 上实际只有一路麦克风有信号,
         * 而原来的降混 mono = (L + R) / 2 把有效信号和一路静音平均了,
         * 直接砍掉一半 —— 再叠加上"gain 命令名冲突导致增益根本没设上",
         * 结果就是 peak 永远只有 50~80, 说话判定永远不触发。
         *
         * 现在改成: 看哪一路强就用哪一路。
         *   - 一边明显强 (3 倍以上) -> 用强的那边
         *   - 两边差不多           -> 取平均 (正常立体声)
         *   - 两边都是底噪         -> 取平均 (都没信号)
         */
        int use_left;    /* 1=用左, 0=用右, -1=取平均 */
        if (rms_l > rms_r * 3 && rms_l > 10) {
            use_left = 1;
        } else if (rms_r > rms_l * 3 && rms_r > 10) {
            use_left = 0;
        } else {
            use_left = -1;
        }

        for (int i = 0; i < frames; i++) {
            if (use_left == 1) {
                mono[i] = stereo[i * 2];
            } else if (use_left == 0) {
                mono[i] = stereo[i * 2 + 1];
            } else {
                mono[i] = (int16_t)(((int)stereo[i * 2] + (int)stereo[i * 2 + 1]) / 2);
            }
        }

        /* 电平统计 (peak/rms) —— 用降混后的单声道, 与上行链路一致 */
        int peak = 0;
        long long sum_sq = 0;
        for (int i = 0; i < frames; i++) {
            int v = mono[i];
            if (v < 0) v = -v;
            if (v > peak) peak = v;
            sum_sq += (long long)mono[i] * mono[i];
        }
        int rms = (int)(sqrt((double)sum_sq / frames));

        s_stats.peak     = (uint32_t)peak;
        s_stats.rms      = (uint32_t)rms;
        s_stats.peak_l   = (uint32_t)peak_l;
        s_stats.peak_r   = (uint32_t)peak_r;
        s_stats.rms_l    = (uint32_t)rms_l;
        s_stats.rms_r    = (uint32_t)rms_r;
        s_stats.samples += (uint32_t)samples;
        s_stats.frames  += (uint32_t)frames;
        s_stats.speaking = (rms > AUDIO_SPEAK_RMS_THRESHOLD);

        /* 写进环形缓冲 (半双工对讲时跳过上行) */
        xSemaphoreTake(s_ring_lock, portMAX_DELAY);
        ring_write_stereo_locked(stereo, frames);
        if (!s_downlink_active) {
            ring_write_mono_locked(mono, frames);
        }
        xSemaphoreGive(s_ring_lock);

        /*
         * 按真实速率喂: 256 帧 @16kHz = 16ms。
         * 读太快会把 RX 的 DMA 队列清空, 导致 TX 那边抢不到 buffer。
         */
        vTaskDelay(pdMS_TO_TICKS((frames * 1000) / AUDIO_SAMPLE_RATE));
    }

    free(stereo);
    free(mono);
    s_capture_task = NULL;
    vTaskDelete(NULL);
}

/* ---------------- 后台任务: 录音电平监测 ----------------
 *
 * 目的: 确认麦克风真的在拾音。
 * 判断方法: 对着麦克风说话时 RMS 会明显抬高; 安静时接近 0。
 * 这比"自检读到了非零数据"更有说服力 —— 后者可能只是底噪。
 *
 * 注意: 这个任务只读统计数字, 不碰 I2S —— I2S 由采集任务独占。
 */
static void audio_monitor_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "录音监测任务已启动 (每 %d ms 报一次电平)", AUDIO_MONITOR_PERIOD_MS);

    uint32_t next_report = (uint32_t)(esp_timer_get_time() / 1000) + AUDIO_MONITOR_PERIOD_MS;

    while (s_monitor_running) {
        uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
        if (now >= next_report) {
            next_report = now + AUDIO_MONITOR_PERIOD_MS;
            ESP_LOGI(TAG, "AUDIO: peak=%d rms=%d L(p=%d r=%d) %s",
                     (int)s_stats.peak, (int)s_stats.rms,
                     (int)s_stats.peak_l, (int)s_stats.peak_r,
                     s_stats.speaking ? "[有声音]" : "[静音]");
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }

    s_monitor_task = NULL;
    vTaskDelete(NULL);
}

esp_err_t audio_board_start_monitor(void)
{
    if (!s_ready) return ESP_FAIL;
    if (s_monitor_task != NULL) return ESP_OK;

    /* 采集任务在 init 里已经起来了 (它是 I2S 的唯一读者) */
    if (s_capture_task == NULL) {
        ESP_LOGE(TAG, "采集任务没在运行, 录音链路不可用");
        return ESP_FAIL;
    }

    s_monitor_running = true;
    BaseType_t ok = xTaskCreatePinnedToCore(audio_monitor_task, "audio_mon", 3072, NULL, 4,
                                             &s_monitor_task, APP_CORE_ID);
    if (ok != pdPASS) {
        s_monitor_running = false;
        s_monitor_task = NULL;
        return ESP_FAIL;
    }
    return ESP_OK;
}

/* ---------------- 后台任务: 循环测试音 ----------------
 *
 * 目的: 确认喇叭真的能出声。I2S 写入成功不代表功放/喇叭正常,
 * 必须让人耳朵听。测试音用 440Hz 正弦波, 1 秒响 2 秒停。
 */
static void audio_tone_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "测试音任务已启动 (440Hz, 每 %d ms 响 %d ms)",
             AUDIO_TONE_PERIOD_MS, AUDIO_TONE_DURATION_MS);

    const int samples = (AUDIO_SAMPLE_RATE * AUDIO_TONE_DURATION_MS) / 1000;

    /*
     * 【2026-11-03 修正】
     * 原本这里一次性分配 1 秒的立体声正弦波: 16000 帧 * 4 字节 = 64KB,
     * 而内部 RAM 当时只剩 28KB —— 分配直接失败, 测试音从来没响过。
     *
     * 改成"小块循环 + 相位连续": 只分配 256 帧 (1KB) 的小块,
     * 每轮填好后按真实速率喂给 DMA, 相位用 double 累加不间断,
     * 听感上和 1 秒大缓冲完全一样, 内存占用从 64KB 降到 1KB。
     */
    const int chunk = 256;                        /* 256 帧 = 16ms @16kHz */
    int16_t *tone = heap_caps_malloc((size_t)chunk * AUDIO_FRAME_BYTES,
                                     MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (tone == NULL) {
        ESP_LOGE(TAG, "测试音任务: 缓冲分配失败 (%d 字节)", chunk * AUDIO_FRAME_BYTES);
        s_tone_running = false;
        s_tone_task = NULL;
        vTaskDelete(NULL);
        return;
    }

    const double step = 2.0 * 3.14159265358979 * 440.0 / AUDIO_SAMPLE_RATE;
    double phase = 0.0;

    while (s_tone_running) {
        /*
         * 分块写并按真实时间节奏喂给 DMA。
         *
         * 为什么不能一口气全写: I2S TX 的 DMA 队列有固定深度, 一次性塞
         * 1 秒的数据 (16000 帧) 会把队列塞满, 之后 i2s_channel_write
         * 拿不到新的 DMA buffer 就只能等 -> 200ms 超时 -> ESP_ERR_TIMEOUT。
         *
         * 按 16kHz 的真实速率喂 (每 16ms 喂 256 帧), DMA 就能持续消费,
         * 写操作永远不阻塞, 声音也是连续平滑的。
         */
        int sent = 0;
        int failed = 0;
        while (sent < samples && s_tone_running) {
            /* 填满一小块正弦波, 相位连续累加 */
            for (int i = 0; i < chunk; i++) {
                int16_t v = (int16_t)(sin(phase) * 12000.0);
                phase += step;
                if (phase > 2.0 * 3.14159265358979) {
                    phase -= 2.0 * 3.14159265358979;
                }
                tone[i * 2]     = v;
                tone[i * 2 + 1] = v;
            }

            int w = audio_board_write(tone, chunk);
            if (w < 0) {
                failed++;
                if (failed > 3) {
                    ESP_LOGW(TAG, "测试音: 连续写入失败, 放弃这一轮");
                    break;
                }
                vTaskDelay(pdMS_TO_TICKS(20));
                continue;
            }
            failed = 0;
            sent += w;
            /* 按喂进去的帧数换算成真实时长来延时, 保证 1 秒的音就是 1 秒 */
            vTaskDelay(pdMS_TO_TICKS((w * 1000) / AUDIO_SAMPLE_RATE));
        }
        s_stats.tone_playing = false;
        int gap = AUDIO_TONE_PERIOD_MS - AUDIO_TONE_DURATION_MS;
        if (gap > 0) {
            vTaskDelay(pdMS_TO_TICKS(gap));
        }
    }

    free(tone);
    s_tone_task = NULL;
    vTaskDelete(NULL);
}

esp_err_t audio_board_start_tone(void)
{
    if (!s_ready) return ESP_FAIL;
    if (s_tone_task != NULL) return ESP_OK;

    s_tone_running = true;
    s_stats.tone_playing = true;
    BaseType_t ok = xTaskCreatePinnedToCore(audio_tone_task, "audio_tone", 4096, NULL, 3,
                                             &s_tone_task, APP_CORE_ID);
    if (ok != pdPASS) {
        s_tone_running = false;
        s_tone_task = NULL;
        return ESP_FAIL;
    }
    return ESP_OK;
}

void audio_board_stop_background(void)
{
    s_monitor_running = false;
    s_tone_running    = false;
    /* 任务自己会 vTaskDelete, 这里只等一小会儿让它退出 */
    for (int i = 0; i < 20 && (s_monitor_task || s_tone_task); i++) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    s_stats.tone_playing = false;
}

void audio_board_stop_tone(void)
{
    s_tone_running = false;
    for (int i = 0; i < 20 && s_tone_task; i++) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    s_stats.tone_playing = false;
}

int audio_board_get_stats(audio_board_stats_t *out)
{
    if (out == NULL) return -1;
    *out = s_stats;
    return 0;
}

/* ---------------- 提示音 (需求文档 §3.8) ---------------- */

/*
 * 播一个【一次性】的提示音。
 *
 * 为什么不用 audio_board_start_tone():
 *   那个是循环测试音 (440Hz, 每 3 秒响 1 秒), 会一直响下去,
 *   而 §3.8 要求的提示音应该是"叮"一声, 然后安静。
 *   而且测试音不响的时候有 2 秒空档, 听起来像坏了。
 *
 * 音色设计:
 *   两个音 (880Hz -> 1174Hz), 各 120ms, 中间 60ms 静音。
 *   这是手机通知音最常见的"叮咚"结构 —— 老人对这个最熟悉,
 *   不会以为是设备坏了。
 *
 * 音量:
 *   峰值 9000 (约 -3dBFS)。太大声会吓到老人 (§3.8 本身就是防惊吓机制),
 *   太小声又听不见。9000 是个折中。
 *
 * 阻塞多久:
 *   总共约 300ms。在按键任务里调用会阻塞 300ms —— 可接受,
 *   因为提示音和按键事件是同一个流程, 顺序执行反而更自然。
 */
void audio_board_play_prompt_beep(void)
{
    if (!s_ready) {
        return;
    }

    /* 提示音期间静音上行, 避免回声被麦克风拾回去 */
    bool was_downlink = audio_board_is_downlink_active();
    audio_board_set_downlink_active(true);

    const int rate = AUDIO_SAMPLE_RATE;
    const int beep_ms = 120;
    const int gap_ms = 60;
    const int beep_samples = (rate * beep_ms) / 1000;
    const int gap_samples = (rate * gap_ms) / 1000;

    /*
     * 【为什么分块生成而不是一次性准备整段缓冲】
     *
     * 整段缓冲 = (2*beep + gap) 帧 * 2 声道 * 2 字节 = 19200 字节。
     * 内部 RAM 紧张 (历史只剩 28KB), 一次性占 19KB 太奢侈。
     *
     * 而且踩过一个坑: 之前算错了缓冲大小 (少算了一半), 多写出去
     * 7680 字节, 把堆冲坏了 —— 人形检测任务随后 LoadProhibited 崩溃,
     * 崩溃点在 esp-dl 的 resize 映射表里 (被踩坏的下标算出越界地址)。
     * 那种 bug 极难定位: 报错在模型推理, 真正的原因在几百行外的音频代码。
     *
     * 分块生成 + 每次只写一小块, 从根上避免了这个问题:
     *   缓冲固定 320 帧 (20ms) = 1280 字节, 栈上都不嫌多。
     */
    const int CHUNK = 320;                 /* 20ms @ 16kHz */
    int16_t chunk[CHUNK * 2];              /* 320 帧 * 2 声道 */

    const double two_pi = 2.0 * 3.14159265358979;
    double phase = 0.0;

    /*
     * 音序用"分段表"描述, 比三层嵌套循环清楚得多, 也不容易写错:
     *   段 0: 880Hz  120ms
     *   段 1: 静音   60ms
     *   段 2: 1174Hz 120ms
     */
    struct {
        double freq;      /* 0 = 静音 */
        int    samples;   /* 该段总样本数 */
    } segs[3] = {
        { 880.0,  beep_samples },
        { 0.0,    gap_samples  },
        { 1174.0, beep_samples },
    };
    const int seg_count = 3;

    int seg_idx = 0;
    int seg_pos = 0;            /* 当前段已写出的样本数 */
    int failed = 0;

    while (seg_idx < seg_count) {
        int n = 0;

        while (n < CHUNK && seg_idx < seg_count) {
            double freq = segs[seg_idx].freq;
            double step = (freq > 0.0) ? (two_pi * freq / rate) : 0.0;

            int16_t v = 0;
            if (freq > 0.0) {
                v = (int16_t)(sin(phase) * 9000.0);
                phase += step;
                if (phase > two_pi) phase -= two_pi;
            }

            chunk[n * 2] = v;
            chunk[n * 2 + 1] = v;
            n++;
            seg_pos++;

            if (seg_pos >= segs[seg_idx].samples) {
                seg_idx++;
                seg_pos = 0;
            }
        }

        if (n == 0) break;

        int w = audio_board_write(chunk, n);
        if (w <= 0) {
            failed++;
            if (failed > 3) {
                ESP_LOGW(TAG, "提示音: 连续写入失败, 中止");
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        failed = 0;
    }

    audio_board_set_downlink_active(was_downlink);

    ESP_LOGI(TAG, "提示音已播放");
}

