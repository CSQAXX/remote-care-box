/*
 * 可靠性模块 —— 需求文档 §3.9
 *
 * ============================ §3.9 要求原文 ============================
 *
 * | 项 | 要求 |
 * | --- | --- |
 * | 心跳 | 每 30s 上报一次设备状态 (RSSI、IP、运行时长、空闲堆、PSRAM 余量) |
 * | 重连 | 指数退避 1s→2s→4s…→上限 60s |
 * | 断网缓存 | 缓存最近 N=20 事件与最近 10 帧 JPEG, 恢复后补传 |
 * | 看门狗 | 任务级看门狗, 10s 未喂狗则复位 |
 * | 异常上报 | 摄像头失效、音频失效、内存不足均上报 error 事件并进入降级模式 |
 * ==================================================================
 *
 * ============================ 为什么要单独一个模块 ============================
 *
 * 这几件事的共同点是: 它们都【不产出新功能】, 只保证"出问题时还能用"。
 *
 * 放在 main.c 里会让 main.c 继续膨胀 (现在已经 5700 行),
 * 而且这些逻辑互相耦合 —— 心跳要读堆和 RSSI, 降级模式要动摄像头和音频,
 * 看门狗要知道各个任务的名字。
 *
 * 拆出来之后:
 *   - 本模块不碰任何业务逻辑, 只做观测和兜底
 *   - 降级模式用【回调】让 main.c 决定"摄像头挂了要不要真的关掉"
 *   - 单元逻辑简单, 容易验证
 *
 * ============================ 降级模式的关键设计 ============================
 *
 * 需求文档 §3.9 明确: "无摄像头时仍可对讲与求助"。
 *
 * 这意味着降级【不能】把整机停掉, 只能停掉"看画面"这一条链路。
 * 音频对讲和按键求助必须照常工作 —— 因为老人出问题时,
 * 能听见子女说话、能按到求助键, 比有画面重要得多。
 *
 * 所以降级模式的具体做法是:
 *   - 停止【重复取帧】(省总线、省电), 但不 deinit 摄像头
 *     (deinit 后再 init 有风险, 而且传感器断电后不一定能醒)
 *   - 上报 error 事件, 让子女知道"画面暂时看不到"
 *   - 音频、按键、屏幕提示全部保持可用
 *
 * 为什么不用 esp_camera_deinit():
 *   之前 `conv 0` 命令直接 deinit, 而生产者任务正在 fb_get() 里,
 *   两者并发直接 RTC_SW_CPU_RST 复位。要安全 deinit 必须先让生产者
 *   退出循环 (s_cam_pause 机制), 再 deinit, 还要能恢复。
 *   这个复杂度在"摄像头偶发取帧失败"这种场景下不值得 ——
 *   偶发失败更可能是总线被抢, 退避一下自己就恢复了。
 * ==================================================================
 */

#ifndef RELIABILITY_H
#define RELIABILITY_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 心跳周期 (需求文档 §3.9: 30s) */
#define RELIABILITY_HEARTBEAT_MS   30000

/* 摄像头取帧连续失败多少次判定为失效 */
#define RELIABILITY_CAM_FAIL_THRESHOLD  50   /* 50 * 20ms = 1s */

/* 低内存判定阈值 */
#define RELIABILITY_LOW_HEAP_BYTES   32768   /* 内部空闲堆 < 32KB 告警 */
#define RELIABILITY_LOW_PSRAM_BYTES  262144  /* PSRAM 空闲 < 256KB 告警 */

/* 设备运行模式 */
typedef enum {
    RELIABILITY_MODE_NORMAL = 0,   /* 一切正常 */
    RELIABILITY_MODE_DEGRADED,      /* 降级: 摄像头不可用, 其余照常 */
} reliability_mode_t;

/**
 * @brief 启动可靠性任务 (心跳 + 健康巡检)
 */
esp_err_t reliability_start(void);

/**
 * @brief 喂一次任务看门狗
 *
 * 每个关键任务在自己的循环里调一次。超过超时时间没喂 -> 触发复位。
 *
 * 只有【已经订阅过】的任务调用才会真正喂狗; 未订阅的任务调用是安全的空操作
 * (不会让 IDF 打 "task not found" 错误日志)。
 */
void reliability_feed_watchdog(void);

/**
 * @brief 把当前任务订阅进任务看门狗
 *
 * 注意: IDF 的 esp_task_wdt_add(NULL) 订阅的是【调用它的那个任务】,
 * 所以这个函数必须由被监控的任务自己调用 (例如在 cam_producer_task
 * 的循环里)。重复调用同一个任务是安全的 (内部去重)。
 *
 * 订阅成功后, 该任务在循环里调 reliability_feed_watchdog() 即可喂狗。
 *
 * @param task_handle  任务句柄 (用于记录名字和去重)
 * @param name         任务名 (日志用)
 * @return ESP_OK 表示已订阅 (或本来就已经订阅过)
 */
esp_err_t reliability_watch_task(TaskHandle_t task_handle, const char *name);

/**
 * @brief 上报一个 error 事件 (会同时进推模式队列和串口日志)
 *
 * @param code   错误码字符串, 例如 "camera_fail"
 * @param detail 补充说明
 */
void reliability_report_error(const char *code, const char *detail);

/**
 * @brief 当前运行模式
 */
reliability_mode_t reliability_get_mode(void);

/**
 * @brief 记一次摄像头取帧失败
 *
 * 连续失败到阈值才上报 error 并进入降级模式。
 * 降级期间音频与按键链路保持可用 (§3.9)。
 */
void reliability_note_cam_failure(void);

/**
 * @brief 记一次摄像头取帧成功 (清零失败计数, 必要时退出降级)
 */
void reliability_note_cam_ok(void);

/**
 * @brief 心跳数据
 */
typedef struct {
    int      rssi;              /* Wi-Fi 信号强度 dBm */
    char     ip[16];            /* 本机 IP (点分十进制, 没连上时 "0.0.0.0") */
    uint64_t uptime_ms;         /* 运行时长 */
    uint32_t free_heap;         /* 内部空闲堆 */
    uint32_t free_psram;        /* PSRAM 空闲 */
    uint32_t psram_size;        /* PSRAM 总量 */
    bool     time_synced;       /* 是否已对时 */
    int64_t  epoch_ms;          /* 当前 epoch 毫秒 (没对过时为 0) */
    bool     wifi_connected;
    bool     camera_ok;
    bool     audio_ok;
    uint32_t cam_fail_count;    /* 连续取帧失败次数 */
} heartbeat_t;

/**
 * @brief 取最近一次心跳数据
 */
void reliability_get_heartbeat(heartbeat_t *out);

/**
 * @brief 把心跳数据序列化成 JSON (给 /health 和推模式用)
 *
 * @param buf      输出缓冲
 * @param buf_size 缓冲大小
 * @param with_replay 是否带 replay 标记 (补传时为 true)
 * @return 写入的字节数
 */
int reliability_heartbeat_to_json(char *buf, size_t buf_size, bool with_replay);

#ifdef __cplusplus
}
#endif

#endif /* RELIABILITY_H */
