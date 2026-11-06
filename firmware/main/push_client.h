/*
 * TCP 推模式客户端 —— 设备主动把事件推给电脑端
 *
 * ============================ 为什么需要这个模块 ============================
 *
 * 可行性验证阶段只有【拉模式】: 浏览器每 500ms 问一次 GET /event,
 * 取走就没了。这个模式有三个硬伤:
 *
 *   1. 【会丢事件】电脑没开页面的时候, 事件进了队列就被下一轮覆盖,
 *      老人按了求助键但没人知道。
 *   2. 【看不出断线】电脑关了 / 网络断了, 设备这边毫无察觉,
 *      还在傻乎乎地往队列里塞事件。
 *   3. 【没法补传】断网期间发生的事, 网络恢复后就没了。
 *
 * 推模式反过来: 设备主动 TCP 连到电脑, 有事就推。
 *   好处是设备能【感知】连接状态 —— 连不上就知道断了, 可以缓存起来等恢复。
 *
 * 这个模块负责三件事 (对应需求文档 §3.9):
 *   1. 维护一条到电脑的长连接, 断了按【指数退避】重连
 *   2. 缓存最近 N=20 个事件 + 最近 10 帧 JPEG
 *   3. 网络恢复后补传, 补传的事件带 "replay":true 标记
 *
 * ============================ 为什么补传要打 replay 标记 ============================
 *
 * 需求文档 §6.3 的抑制规则: 补传的事件【不能】再触发一次告警。
 *
 * 场景: 老人 10:00 按了求助键, 当时家里断网, 10:05 网络恢复补传。
 * 如果补传的事件和实时事件一视同仁, 子女会在 10:05 收到一条"刚才发生了求助"
 * 的告警 —— 但他可能在 10:01 已经通过别的方式知道了, 这就是重复告警,
 * 会让人对通知脱敏 (需求文档 RK-06 的核心风险)。
 *
 * 所以补传的事件必须带 "replay":true, 由云端规则引擎识别并抑制告警,
 * 但【仍然入库】—— 因为"那天老人确实按过按钮"是重要的事实, 只是不该再响一次。
 *
 * ============================ 线路格式 ============================
 *
 * 一条 TCP 流上混两种帧, 用换行分隔:
 *
 *   事件 (JSON, 一行一个):
 *       {"type":"button","msg":"老人按了求助按钮","seq":123,"ts":1700000000000}
 *       {"type":"button","msg":"...","replay":true,"seq":124,"ts":...}   <- 补传
 *
 *   JPEG (JSON 头一行 + 原始字节 + 换行):
 *       {"type":"jpeg","seq":9,"len":12345,"ts":...}\n<12345 字节>\n
 *
 * 为什么不把 JPEG 转成 base64 塞进 JSON:
 *   base64 会让体积涨 33%, 而且 ESP32-S3 上做 base64 编码要吃 CPU,
 *   会直接影响 JPEG 编码的帧率。这个模块在 PSRAM 里已经有原始字节了,
 *   直接发原始字节最省。
 *
 * 电脑端的解析见 pc_receiver/recv_event.py。
 * ==================================================================
 */

#ifndef PUSH_CLIENT_H
#define PUSH_CLIENT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- 缓存规模 (需求文档 §3.9: N=20 事件 + 10 帧 JPEG) ---- */
#define PUSH_EVENT_MAX       20   /* 缓存的事件条数 */
#define PUSH_EVENT_BODY_MAX  288  /* 单条事件的 JSON 主体长度 (不含外层 {}) */

/* JPEG 缓存用静态数组 + PSRAM 池, 这里只声明条数 */
#define PUSH_JPEG_MAX        10

/* ---- 退避策略 (需求文档 §3.9: 1s -> 2s -> 4s -> ... -> 上限 60s) ---- */
#define PUSH_BACKOFF_MIN_MS  1000
#define PUSH_BACKOFF_MAX_MS  60000

/**
 * @brief 启动推模式客户端任务
 *
 * 会在后台建一个任务, 尝试连到 camera_pins.h 里配的 PC_RECEIVER_IP:PORT。
 * 连不上不会阻塞其他模块, 只是后台一直按退避策略重试。
 */
esp_err_t push_client_start(void);

/**
 * @brief 当前是否已连上电脑
 */
bool push_client_is_connected(void);

/**
 * @brief 提交一个事件, 由本模块负责发出去
 *
 * @param body  JSON 【主体】, 不含最外层的 {}。例如
 *              "type":"button","msg":"老人按了求助按钮","seq":1,"ts":1700000000000
 *
 * @return  true = 已入队 (不代表已发出)
 */
bool push_client_submit_event(const char *body);

/**
 * @brief 提交一帧 JPEG
 *
 * 内部会拷贝到 PSRAM 环形缓冲 (最多 PUSH_JPEG_MAX 帧),
 * 超出就覆盖最旧的。
 *
 * @return true = 已入队
 */
bool push_client_push_jpeg(const uint8_t *data, size_t len);

/**
 * @brief 取统计数据 (给 /health 用)
 */
typedef struct {
    uint32_t sent_events;    /* 累计发出的事件数 */
    uint32_t sent_jpegs;     /* 累计发出的 JPEG 帧数 */
    uint32_t replayed;       /* 累计补传的事件数 */
    uint32_t dropped;        /* 因队列满被丢弃的事件数 */
    uint32_t queued;         /* 当前队列里还剩多少条 */
    uint32_t connect_fails;  /* 累计连接失败次数 */
    bool     connected;      /* 当前是否已连接 */
    uint32_t backoff_ms;     /* 当前退避时长 */
} push_stats_t;

void push_client_get_stats(push_stats_t *out);

#ifdef __cplusplus
}
#endif

#endif /* PUSH_CLIENT_H */
