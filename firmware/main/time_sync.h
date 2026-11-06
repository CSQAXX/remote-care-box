/*
 * 网络对时 (SNTP) —— 对外接口
 *
 * 设备没有 RTC, 开机后系统时间是编译时刻。所有需要真实时间的
 * 功能 (活动检测 last_active_ts、告警时间戳、夜间规则) 都依赖这里。
 *
 * 关联需求文档 §3.9 (可靠性) 与 §6.3 (规则引擎依赖时间)
 */

#ifndef TIME_SYNC_H
#define TIME_SYNC_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 启动对时任务
 *
 * 应在 Wi-Fi 连上之后调用 (或者更早, 任务内部会等网络)。
 */
esp_err_t time_sync_start(void);

/**
 * @brief 是否已经成功对过时
 */
bool time_sync_is_synced(void);

/**
 * @brief 当前 epoch 毫秒
 *
 * 没对过时返回 0 —— 调用方【必须】把 0 当作"时间未知"处理,
 * 不要拿它当时间戳用。
 */
int64_t time_sync_epoch_ms(void);

/**
 * @brief 开机时长 (毫秒)
 */
int64_t time_sync_uptime_ms(void);

#ifdef __cplusplus
}
#endif

#endif /* TIME_SYNC_H */
