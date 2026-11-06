/*
 * 活动检测 —— 实现
 *
 * 见 activity_detect.h 的说明 (需求文档 §3.5)。
 *
 * ============================ 设计要点 ============================
 *
 * 1) 两路信号, 互相独立:
 *      运动 (motion)      = 帧间差分, 纯软件, 极快 (1~2ms)
 *      人形 (human)       = esp-dl pedestrian_detect, 125ms/帧
 *
 *    为什么分开:
 *      §3.5 要求"连续 N 帧检出人/运动 -> 置有活动"。但两者的
 *      可靠性和成本差 60 倍。差分能秒级响应, 人形模型能给置信度。
 *      分开后可以各自调阈值, 且人形模型加载失败时运动检测照样能工作。
 *
 * 2) 降采样到 40x30 做差分:
 *      320x240 全量差分要遍历 76800 像素, 在 240MHz 上不便宜。
 *      每 8 个像素取 1 个 (40x30 = 1200 点), 差分只要 1200 次减法,
 *      足够反映"画面变了没有"。亮度均值也在这 1200 点上算。
 *
 * 3) 低光保护 (low_light) —— 这是防误报的关键:
 *      §3.5 明确要求"画面整体亮度低于阈值时判为光线不足,
 *      此时不判定为无活动, 仅记录 low_light 标记"。
 *
 *      为什么必须这样: 老人晚上关灯睡觉, 画面全黑 -> 差分=0 ->
 *      如果当成"没活动", 规则引擎会在夜里报"老人长时间没动"。
 *      这是最典型的误报, 会让子女直接关掉通知 (RK-06)。
 *
 * 4) 只上报事实, 不做超时阈值判断 (§2.3 原则 1):
 *      "连续 M 秒无运动 -> 置静止"这个阈值属于后台规则引擎。
 *      设备端只负责如实上报 motion / human_present / low_light,
 *      阈值由 §6.3 的 R-03 / R-04 配置。
 *
 * 5) esp-dl 是 C++ 库, 所以本模块用 .cpp 编译。
 * ==================================================================
 */

#include <string.h>
#include <math.h>
#include <time.h>
#include <list>
#include <vector>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_check.h"

#include "activity_detect.h"
#include "time_sync.h"

static const char *TAG = "actdet";

/* esp-dl 相关 (只在 ENABLE_HUMAN_DETECT 时引入) */
#if ENABLE_HUMAN_DETECT
#include "dl_image_define.hpp"
#include "dl_detect_define.hpp"
#include "pedestrian_detect.hpp"
#endif

/* ---------------- 内部状态 ---------------- */

static activity_state_t   s_state;
static bool               s_initialized = false;

/* 降采样缓存: 40x30 的亮度值 */
#define DS_W  40
#define DS_H  30
static uint8_t s_ds_prev[DS_W * DS_H];
static uint8_t s_ds_cur[DS_W * DS_H];
static bool    s_ds_valid = false;      /* 上一帧降采样数据是否有效 */

/* 人形检测用的降采样 RGB888 缓冲 (esp-dl 要 224x224, 但它自己会 resize) */
#if ENABLE_HUMAN_DETECT
static uint8_t *s_rgb_buf = NULL;      /* PSRAM */

/*
 * esp-dl 检测器。
 *
 * 单独放在 .cpp 的静态变量里 —— PedestrianDetect 是 C++ 类型,
 * 不能出现在 C 兼容的 activity_state_t 结构体里。
 *
 * 生命周期: 进程级, 不释放 (嵌入式没这个必要)。
 */
static PedestrianDetect *s_detector = NULL;
#endif

/* ---------------- 降采样 ---------------- */

/*
 * 把 RGB565 (大端, 摄像头原始格式) 降采样成 40x30 的亮度值。
 *
 * RGB565 大端格式: 字节序 [hi, lo], hi = RRRRRGGG, lo = GGGGBBBB
 * 亮度用经典的 Y = (R*299 + G*587 + B*114) / 1000。
 *
 * 大端 vs 小端: main.c 里 lcd_draw_rgb565(..., swap_bytes=true) 说明
 * 摄像头给的是大端 RGB565。这里必须按大端解析, 否则亮度算错,
 * 低光判定会误触发。
 */
static void downsample_luma(const uint8_t *rgb565_be,
                            int width, int height,
                            uint8_t *out)
{
    const int step_x = width / DS_W;
    const int step_y = height / DS_H;

    for (int dy = 0; dy < DS_H; dy++) {
        int sy = dy * step_y;
        const uint8_t *row = rgb565_be + (size_t)sy * width * 2;
        for (int dx = 0; dx < DS_W; dx++) {
            int sx = dx * step_x;
            uint8_t hi = row[sx * 2];
            uint8_t lo = row[sx * 2 + 1];

            uint32_t r = (hi >> 3) & 0x1F;    /* 5 bit */
            uint32_t g = ((hi & 0x07) << 2) | (lo >> 6);  /* 3+2 bit */
            uint32_t b = lo & 0x3F;            /* 6 bit */

            /* 5/6 位转 8 位 */
            r = (r * 255) / 31;
            g = (g * 255) / 63;
            b = (b * 255) / 63;

            uint32_t y = (r * 299 + g * 587 + b * 114) / 1000;
            out[dy * DS_W + dx] = (uint8_t)(y > 255 ? 255 : y);
        }
    }
}

/* ---------------- 运动检测 ---------------- */

/*
 * 帧间差分 -> 运动像素占比。
 *
 * 判据: |cur - prev| > DIFF_THRESH 的点算"变了"。
 * 阈值 12 是经验值 —— 低于它会被 GC0308 的 AE 噪声触发 (画面
 * 整体亮度会随自动曝光上下波动), 高于它真人走动的小幅度变化会漏。
 */
#define DIFF_THRESH      12

static float motion_ratio(const uint8_t *prev, const uint8_t *cur)
{
    int changed = 0;
    for (int i = 0; i < DS_W * DS_H; i++) {
        int d = (int)cur[i] - (int)prev[i];
        if (d < 0) d = -d;
        if (d > DIFF_THRESH) {
            changed++;
        }
    }
    return (float)changed / (float)(DS_W * DS_H);
}

/* ---------------- 人形检测 ---------------- */

#if ENABLE_HUMAN_DETECT

/*
 * RGB565BE -> RGB888, 顺便把尺寸缩到 esp-dl 友好的尺寸。
 *
 * esp-dl 的 pedestrian_detect 输入是 224x224x3, 但它的
 * ImagePreprocessor 会自己 resize, 所以我们只要给它一个
 * 连续的 RGB888 缓冲就行, 给多大的分辨率它都能处理。
 *
 * 给 160x120 而不是 320x240:
 *   1) 预处理耗时与输入面积成正比, 减半能省几 ms
 *   2) 人形检测只需要"有没有人", 不需要细节
 *
 * 缓冲必须放 PSRAM —— 160*120*3 = 57.6KB, 内部 RAM 放不下。
 */
#define HD_W 160
#define HD_H 120

static bool ensure_rgb_buf(void)
{
    if (s_rgb_buf == NULL) {
        s_rgb_buf = (uint8_t *)heap_caps_malloc(HD_W * HD_H * 3,
                                                MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_rgb_buf == NULL) {
            ESP_LOGE(TAG, "RGB 缓冲分配失败 (PSRAM)");
            return false;
        }
        ESP_LOGI(TAG, "人形检测缓冲已分配 (PSRAM, %d 字节)", HD_W * HD_H * 3);
    }
    return true;
}

static void rgb565be_to_rgb888(const uint8_t *src, int sw, int sh,
                               uint8_t *dst, int dw, int dh)
{
    for (int y = 0; y < dh; y++) {
        int sy = y * sh / dh;
        const uint8_t *srow = src + (size_t)sy * sw * 2;
        uint8_t *drow = dst + (size_t)y * dw * 3;
        for (int x = 0; x < dw; x++) {
            int sx = x * sw / dw;
            uint8_t hi = srow[sx * 2];
            uint8_t lo = srow[sx * 2 + 1];

            uint32_t r = ((hi >> 3) & 0x1F) * 255 / 31;
            uint32_t g = (((hi & 0x07) << 2) | (lo >> 6)) * 255 / 63;
            uint32_t b = (lo & 0x3F) * 255 / 63;

            drow[x * 3 + 0] = (uint8_t)r;
            drow[x * 3 + 1] = (uint8_t)g;
            drow[x * 3 + 2] = (uint8_t)b;
        }
    }
}

#endif /* ENABLE_HUMAN_DETECT */

/* ---------------- 对外接口 ---------------- */

esp_err_t activity_detect_init(void)
{
    if (s_initialized) {
        return ESP_OK;
    }

    memset(&s_state, 0, sizeof(s_state));
    s_ds_valid = false;

#if ENABLE_HUMAN_DETECT
    if (ensure_rgb_buf()) {
        /*
         * lazy_load = true: 模型在第一次 run() 时才加载。
         * 这样开机阶段不会因为加载模型 (435KB) 卡住摄像头启动。
         *
         * 注意: 本工程编译 esp-dl 时用的是 -fno-exceptions (IDF 默认),
         * 所以不能用 try/catch。pedestrian_detect 的构造函数在模型
         * 缺失时只打 ESP_LOGE 并把 m_model 留成 nullptr,
         * 不会抛异常 —— 所以直接 new 就行, 之后靠 get_raw_model()
         * 判断是否真的加载成功。
         */
        s_detector = new PedestrianDetect(
            PedestrianDetect::PICO_S8_V1, /* lazy_load = */ true);
        s_state.has_detector = (s_detector != nullptr);
        if (s_state.has_detector) {
            ESP_LOGI(TAG, "人形检测器已创建 (pedestrian_detect pico_s8_v1, 延迟加载)");
        }
    } else {
        ESP_LOGW(TAG, "无法分配人形检测缓冲, 降级为纯运动检测");
    }
#else
    ESP_LOGW(TAG, "ENABLE_HUMAN_DETECT=0, 只做运动检测");
#endif

    s_initialized = true;
    ESP_LOGI(TAG, "活动检测已初始化 (分析频率 %d fps, 低光阈值 %d, 运动阈值 %.2f)",
             ACTIVITY_ANALYZE_FPS, ACTIVITY_LOW_LIGHT_THRESHOLD, ACTIVITY_MOTION_THRESHOLD);
    return ESP_OK;
}

/*
 * 分析一帧。
 *
 * @param rgb565_be  摄像头原始帧 (大端 RGB565)
 * @param width      帧宽
 * @param height     帧高
 */
void activity_detect_analyze(const uint8_t *rgb565_be, int width, int height)
{
    if (!s_initialized || rgb565_be == NULL || width <= 0 || height <= 0) {
        return;
    }

    const int64_t now_ms = esp_timer_get_time() / 1000;

    /* ---- 节流: §3.5 要求按 2fps 分析 ---- */
    int64_t interval_ms = 1000 / ACTIVITY_ANALYZE_FPS;
    if (s_state.last_analyze_ms != 0 &&
        (now_ms - s_state.last_analyze_ms) < interval_ms) {
        return;
    }
    s_state.last_analyze_ms = now_ms;
    s_state.analyze_count++;

    /* ---- 1) 降采样 + 亮度 ---- */
    downsample_luma(rgb565_be, width, height, s_ds_cur);

    uint32_t luma_sum = 0;
    for (int i = 0; i < DS_W * DS_H; i++) {
        luma_sum += s_ds_cur[i];
    }
    uint8_t avg_luma = (uint8_t)(luma_sum / (DS_W * DS_H));
    s_state.avg_luma = avg_luma;

    /* ---- 2) 低光判定 ---- */
    bool low_light = (avg_luma < ACTIVITY_LOW_LIGHT_THRESHOLD);
    if (low_light != s_state.low_light) {
        ESP_LOGI(TAG, "光线状态变化: %s (平均亮度 %d)",
                 low_light ? "光线不足" : "光线正常", avg_luma);
    }
    s_state.low_light = low_light;

    /* ---- 3) 运动检测 ---- */
    if (!s_ds_valid) {
        /* 第一帧没有"上一帧", 只能记亮度, 不判运动 */
        s_ds_valid = true;
        memcpy(s_ds_prev, s_ds_cur, sizeof(s_ds_prev));
        s_state.motion = false;
        s_state.motion_ratio = 0.0f;
        s_state.human_present = false;
        s_state.human_score = 0.0f;
        return;
    }

    float ratio = motion_ratio(s_ds_prev, s_ds_cur);
    memcpy(s_ds_prev, s_ds_cur, sizeof(s_ds_prev));
    s_state.motion_ratio = ratio;

    /*
     * 运动判定:
     *   - 低光时不算运动 (画面全黑 -> 差分全是噪声)
     *   - 比例超过阈值才算动
     */
    if (low_light) {
        s_state.motion = false;
    } else {
        s_state.motion = (ratio >= ACTIVITY_MOTION_THRESHOLD);
    }

    /* ---- 4) 人形检测 ---- */
    s_state.human_present = false;
    s_state.human_score = 0.0f;

#if ENABLE_HUMAN_DETECT
    if (s_state.has_detector && s_detector != nullptr) {
        rgb565be_to_rgb888(rgb565_be, width, height, s_rgb_buf, HD_W, HD_H);

        dl::image::img_t img = {
            .data = s_rgb_buf,
            .width = HD_W,
            .height = HD_H,
            .pix_type = dl::image::DL_IMAGE_PIX_TYPE_RGB888,
        };

        /*
         * run() 内部会 lazy-load 模型 (第一次调用时)。
         * 模型加载失败时 m_model 是 nullptr, DetectWrapper::run 会
         * 再次调 load_model(), 然后 m_model->run() 就崩了。
         *
         * 所以这里先 get_raw_model() 触发加载并检查结果 ——
         * 加载失败就永久降级, 不再尝试, 避免每次分析都崩。
         */
        dl::Model *raw = s_detector->get_raw_model(0);
        if (raw == nullptr) {
            ESP_LOGE(TAG, "人形模型加载失败, 永久降级为纯运动检测");
            s_state.has_detector = false;
        } else {
            std::list<dl::detect::result_t> &res = s_detector->run(img);

            float best = 0.0f;
            for (auto &r : res) {
                if (r.score > best) {
                    best = r.score;
                }
            }
            s_state.human_score = best;
            s_state.human_present = (best >= ACTIVITY_HUMAN_THRESHOLD);
        }
    }
#endif

    /* ---- 5) 活动状态与时间戳 ---- */
    /*
     * §3.5: 连续 N 帧 (建议 3) 检出人/运动 -> 置"有活动", 刷新 last_active_ts。
     *
     * 注意 last_active_ts 只在【确认有活动】时刷新, 低光帧不刷 ——
     * 否则夜里画面全黑时会一直"刷新活动时间", 那就永远检测不到
     * 老人没动了, 规则引擎的 R-04 (夜间 8h 无活动) 就永远不会触发。
     */
    bool any_signal = s_state.motion || s_state.human_present;
    if (any_signal && !low_light) {
        s_state.active_streak++;
        if (s_state.active_streak >= ACTIVITY_ACTIVE_FRAMES) {
            s_state.has_activity = true;
            s_state.last_active_ms = now_ms;
            s_state.state_change_count++;
        }
    } else {
        s_state.active_streak = 0;
    }

    /*
     * §3.5: 连续 M 秒 (建议 30s) 无任何运动 -> 置"静止"。
     *
     * 但低光时【不判定为无活动】(原文: "此时不判定为无活动, 仅记录 low_light 标记")。
     * 所以低光时把静默计时器清零, 并且保持 has_activity 不变。
     */
    if (!any_signal && !low_light) {
        if (s_state.idle_since_ms == 0) {
            s_state.idle_since_ms = now_ms;
        }
        if ((now_ms - s_state.idle_since_ms) >= ACTIVITY_IDLE_MS) {
            if (s_state.has_activity) {
                ESP_LOGI(TAG, "连续 %d ms 无活动 -> 置为静止", ACTIVITY_IDLE_MS);
            }
            s_state.has_activity = false;
            s_state.state_change_count++;
        }
    } else {
        s_state.idle_since_ms = 0;
    }

    /* ---- 6) 变了才记日志, 免得刷屏 ---- */
    if (s_state.motion != s_state.last_log_motion ||
        s_state.human_present != s_state.last_log_human ||
        s_state.low_light != s_state.last_log_low) {
        ESP_LOGI(TAG, "活动: motion=%d(%.3f) human=%d(%.2f) low_light=%d luma=%d streak=%d",
                 (int)s_state.motion, (float)s_state.motion_ratio,
                 (int)s_state.human_present, (float)s_state.human_score,
                 (int)s_state.low_light, avg_luma,
                 (int)s_state.active_streak);
        s_state.last_log_motion = s_state.motion;
        s_state.last_log_human = s_state.human_present;
        s_state.last_log_low = s_state.low_light;
    }
}

/* 取当前状态 */
const activity_state_t *activity_detect_get_state(void)
{
    return &s_state;
}

/*
 * 生成 §3.5 规定格式的 JSON。
 *
 * 原文:
 *   { "event":"activity", "human_present":true|false, "motion":true|false,
 *     "last_active_ts":<epoch_ms>, "score":<float> }
 *
 * 额外加了两个字段:
 *   "low_light"  —— §3.5 要求记录的光线不足标记
 *   "analyze_count" —— 分析了多少帧 (调试用)
 */
int activity_detect_to_json(char *buf, size_t buf_size)
{
    if (buf == NULL || buf_size == 0) {
        return 0;
    }

    /*
     * last_active_ts 要的是 epoch 毫秒。
     *
     * 用 time_sync 模块换算, 而不是 time(NULL) ——
     * 设备没 RTC, 没对过时 time(NULL) 返回的是编译时刻,
     * 换算出来的 epoch 是个看起来像真的但完全错误的值。
     *
     * time_sync_epoch_ms() 在没对过时返回 0, 这里直接透传 0,
     * 云端收到 ts=0 就知道"设备时间未知"。
     */
    int64_t now_epoch = time_sync_epoch_ms();
    int64_t last_active_epoch = 0;
    if (now_epoch > 0 && s_state.last_active_ms > 0) {
        last_active_epoch = now_epoch - (time_sync_uptime_ms() - s_state.last_active_ms);
    }

    int n = snprintf(buf, buf_size,
                     "{\"event\":\"activity\","
                     "\"human_present\":%s,"
                     "\"motion\":%s,"
                     "\"low_light\":%s,"
                     "\"motion_ratio\":%.3f,"
                     "\"score\":%.2f,"
                     "\"avg_luma\":%d,"
                     "\"last_active_ts\":%lld,"
                     "\"analyze_count\":%u}",
                     s_state.human_present ? "true" : "false",
                     s_state.motion ? "true" : "false",
                     s_state.low_light ? "true" : "false",
                     (double)s_state.motion_ratio,
                     (double)s_state.human_score,
                     (int)s_state.avg_luma,
                     (long long)last_active_epoch,
                     (unsigned)s_state.analyze_count);
    return (n < 0) ? 0 : ((size_t)n >= buf_size ? (int)buf_size - 1 : n);
}
