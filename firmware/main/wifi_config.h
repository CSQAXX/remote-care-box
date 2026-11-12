/*
 * wifi_config.h —— WiFi 配置存 NVS (换网不用重新烧固件)
 *
 * 见 wifi_config.c 顶部的完整设计说明。
 */

#ifndef WIFI_CONFIG_H
#define WIFI_CONFIG_H

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 从 NVS 读 WiFi 配置
 *
 * 开机时调用。读到就返回 true 并覆盖当前配置; 没读到返回 false,
 * 调用方应该继续用编译期默认值 (camera_pins.h 的 WIFI_SSID/WIFI_PASSWORD)。
 */
bool wifi_config_load(void);

/**
 * @brief 保存 WiFi 配置到 NVS
 *
 * @param ssid  网络名 (不能为空)
 * @param pass  密码; NULL 或空串 = 开放网络
 *
 * @return ESP_OK 表示已写入并提交 (掉电安全)
 */
esp_err_t wifi_config_save(const char *ssid, const char *pass);

/**
 * @brief 删掉 NVS 里的配置, 恢复用编译期默认值
 */
esp_err_t wifi_config_forget(void);

/** @brief 当前生效的 SSID (NVS 优先, 失败时是编译期默认值) */
const char *wifi_config_ssid(void);

/** @brief 当前生效的密码 (开放网络时是空串) */
const char *wifi_config_pass(void);

/** @brief 当前配置是不是来自 NVS (false = 编译期默认值) */
bool wifi_config_from_nvs(void);

#ifdef __cplusplus
}
#endif

#endif /* WIFI_CONFIG_H */
