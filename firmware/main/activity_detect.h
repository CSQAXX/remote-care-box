/*
 * 活动检测 —— 对外接口
 *
 * ============================ 需求原文 ============================
 * v1.0 开发需求文档 §3.5 活动检测规格
 *
 * - 采样：摄像头出图降采样至 QVGA/VGA，按 2 fps 分析；
 *   人形检测用 esp-dl 预训练模型（INT8 量化）。
 * - 输出事件（type=0x03 的子类型 activity）：
 *   { "event":"activity", "human_present":true|false, "motion":true|false,
 *     "last_active_ts":<epoch_ms>, "score":<float> }
 * - 判定规则：连续 N 帧（建议 3 帧）检出人/运动 → 置「有活动」，
 *   刷新 last_active_ts；连续 M 秒（建议 30s）无任何运动 → 置「静止」。
 * - 夜间/熄灯场景：画面整体亮度低于阈值时，判为「光线不足」，
 *   此时不判定为无活动（避免误报），仅记录 low_light 标记。
 * - 设备端只上报事实，不做超时阈值判断（阈值属于后台规则）。
 * ==================================================================
 */

#ifndef ACTIVITY_DETECT_H
#define ACTIVITY_DETECT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- 分析参数 (对应 §3.5 的"建议"值) ---- */

/* 分析频率: §3.5 要求按 2fps 分析 */
#define ACTIVITY_ANALYZE_FPS        2

/* 连续多少帧有信号才置「有活动」(§3.5 建议 3 帧) */
#define ACTIVITY_ACTIVE_FRAMES      3

/* 连续多少 ms 无信号才置「静止」(§3.5 建议 30s) */
#define ACTIVITY_IDLE_MS            30000

/* 运动判定阈值: 变化像素占比 */
#define ACTIVITY_MOTION_THRESHOLD   0.02f

/* 人形判定阈值: esp-dl 检测置信度 */
#define ACTIVITY_HUMAN_THRESHOLD    0.5f

/*
 * 低光阈值: 降采样后 40x30 的平均亮度。
 *
 * 取值依据: 0~255 的 8 位亮度。室内正常照明一般 > 60,
 * 关灯/夜灯下 < 30。这个阈值下, 白天拉窗帘也基本不会误判低光。
 */
#define ACTIVITY_LOW_LIGHT_THRESHOLD 35

/*
 * 是否启用 esp-dl 人形检测。
 *
 * 关掉也能工作 —— 只做运动检测, human_present 恒为 false。
 * 用于 esp-dl 拉不下来时的降级路径。
 */
#ifndef ENABLE_HUMAN_DETECT
#define ENABLE_HUMAN_DETECT 1
#endif

/**
 * @brief 活动状态快照
 */
typedef struct {
    bool     motion;              /* 是否有运动 */
    float    motion_ratio;        /* 变化像素占比 0.0~1.0 */
    bool     human_present;       /* 是否检出人 */
    float    human_score;         /* 人形检测置信度 0.0~1.0 */
    bool     low_light;           /* 是否光线不足 */
    uint8_t  avg_luma;            /* 平均亮度 0~255 */
    bool     has_activity;        /* 当前是否处于「有活动」状态 */
    uint32_t active_streak;       /* 连续有信号的帧数 */
    uint32_t analyze_count;       /* 累计分析帧数 */
    uint32_t state_change_count;  /* 状态变化次数 */
    int64_t  last_active_ms;      /* 最近一次确认活动的时刻 (开机毫秒) */
    int64_t  idle_since_ms;       /* 进入静默的起始时刻, 0 = 未静默 */
    int64_t  last_analyze_ms;     /* 上次分析时刻 (节流用) */

    /* 仅记录状态变化日志用 */
    bool     last_log_motion;
    bool     last_log_human;
    bool     last_log_low;

    /*
     * esp-dl 检测器是否可用。
     *
     * 检测器指针本身是 C++ 类型 (PedestrianDetect*), 放在 .cpp 里的
     * 静态变量中, 这个 C 兼容的结构体只记一个标志位。
     */
    bool     has_detector;
} activity_state_t;

/**
 * @brief 初始化活动检测模块
 *
 * 必须在摄像头初始化之后调用。
 */
esp_err_t activity_detect_init(void);

/**
 * @brief 分析一帧, 更新活动状态
 *
 * @param rgb565_be  摄像头原始帧缓冲 (大端 RGB565, 就是 fb->buf)
 * @param width      帧宽
 * @param height     帧高
 *
 * @note 内部按 ACTIVITY_ANALYZE_FPS 节流, 调用多快都只按 2fps 算。
 */
void activity_detect_analyze(const uint8_t *rgb565_be, int width, int height);

/**
 * @brief 取当前状态快照
 */
const activity_state_t *activity_detect_get_state(void);

/**
 * @brief 生成 §3.5 规定格式的 JSON
 *
 * @return 写入的字节数 (不含结尾 '\0')
 */
int activity_detect_to_json(char *buf, size_t buf_size);

#ifdef __cplusplus
}
#endif

#endif /* ACTIVITY_DETECT_H */
