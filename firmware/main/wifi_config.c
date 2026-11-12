/*
 * wifi_config.c —— WiFi 配置存 NVS, 换网不用重新烧固件
 *
 * ============================ 解决什么 ============================
 *
 * 原来 SSID / 密码是编译期常量 (camera_pins.h 里的 WIFI_SSID / WIFI_PASSWORD),
 * 换一次网络就要:
 *   1. 改源码
 *   2. idf.py build (几分钟)
 *   3. 整片烧录 (两分钟, 期间盒子是哑巴的)
 *
 * 老人在家的盒子, 校园网/路由器换频段、换名字都很常见, 这个流程太重。
 *
 * 现在改成: 配置存在 NVS (Flash 里的非易失存储), 开机时优先读 NVS,
 * 读不到再退回编译期默认值。改配置只要一条串口命令, 不刷机。
 *
 * ============================ 为什么存 NVS 而不是 Flash 文件 ============================
 *
 * NVS 有现成的掉电保护 (写操作是原子提交的, 断电不会写坏),
 * 而且不占用额外的 Flash 分区 (本工程 16MB Flash 已经切得很碎)。
 *
 * ============================ 存储格式 ============================
 *
 * NVS 分区 "nvs", namespace "wifi_cfg":
 *   "ssid"   -> SSID 字符串
 *   "pass"   -> 密码字符串 (没设密码就整条删除)
 *   "ver"    -> 配置版本号 (uint32), 目前是 1
 *
 * 用 "ver" 是为了以后加配置项 (比如静态 IP / 隐藏 SSID 开关) 时
 * 能平滑升级, 不用清空整个 namespace。
 *
 * ============================ 用法 ============================
 *
 * 串口 (115200):
 *   wifiset <SSID> [密码]    设置并立即重连
 *   wifisave <SSID> [密码]   只保存, 不重连
 *   wifishow                 看当前生效的配置 (含来源: NVS 还是默认值)
 *   wififorgot               删掉 NVS 里的配置, 恢复用编译期默认值
 *
 *   密码省略 = 该网络是开放网络 (OPEN)。校园网常见。
 *
 * 网页 (等下会加):
 *   POST /wifi  {"ssid":"...", "pass":"..."}
 */

#include "wifi_config.h"

#include <string.h>

#include "esp_log.h"
#include "esp_system.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "camera_pins.h"

static const char *TAG = "wifi_cfg";

#define NVS_NS       "wifi_cfg"
#define NVS_KEY_SSID "ssid"
#define NVS_KEY_PASS "pass"
#define NVS_KEY_VER  "ver"

#define WIFI_CFG_VER 1

/* SSID 最长 32 字节 (802.11 限制), 密码最长 64 字节 (WPA2) */
#define SSID_MAX_LEN 32
#define PASS_MAX_LEN 64

/* 当前生效的配置。来源标记用于 /health 和串口展示。 */
static char s_ssid[SSID_MAX_LEN + 1] = {0};
static char s_pass[PASS_MAX_LEN + 1] = {0};
static bool s_from_nvs = false;

/*
 * 编译期默认值的兜底。
 *
 * 【为什么需要】
 * wifi_config_load() 是在 app_main 里调的, 但 wifi_init() 之类的地方
 * 可能先被调用; 而且 wifishow 这类命令在没读到 NVS 时也得显示点什么。
 * 所以这里不能返回空串 —— 得回落到 camera_pins.h 里的默认值。
 */
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif

bool wifi_config_from_nvs(void)
{
    return s_from_nvs;
}

const char *wifi_config_ssid(void)
{
    /* 没读到 NVS 就回落到编译期默认值 —— 不能返回空串 */
    return (s_ssid[0] != '\0') ? s_ssid : WIFI_SSID;
}

const char *wifi_config_pass(void)
{
    if (s_ssid[0] != '\0') {
        return s_pass;
    }
    return WIFI_PASSWORD;
}

/*
 * 从 NVS 读配置。
 *
 * 读不到 (namespace 不存在 / 键不存在) 是【正常情况】, 返回 false 让
 * 调用方退回编译期默认值 —— 不是错误。
 */
bool wifi_config_load(void)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READONLY, &h);
    if (err != ESP_OK) {
        ESP_LOGI(TAG, "NVS 里还没有 WiFi 配置 (nvs_open: %s), 用编译期默认值",
                 esp_err_to_name(err));
        s_from_nvs = false;
        return false;
    }

    size_t len = sizeof(s_ssid);
    err = nvs_get_str(h, NVS_KEY_SSID, s_ssid, &len);
    if (err != ESP_OK) {
        ESP_LOGI(TAG, "NVS 里没有 SSID, 用编译期默认值");
        nvs_close(h);
        s_from_nvs = false;
        return false;
    }
    s_ssid[SSID_MAX_LEN] = '\0';

    /*
     * 密码是可选的 —— 开放网络没有密码。
     * 读不到密码不算失败, 反而是"成功"(配置了一个开放网络)。
     */
    len = sizeof(s_pass);
    if (nvs_get_str(h, NVS_KEY_PASS, s_pass, &len) != ESP_OK) {
        s_pass[0] = '\0';
    }
    s_pass[PASS_MAX_LEN] = '\0';

    nvs_close(h);
    s_from_nvs = true;

    ESP_LOGI(TAG, "从 NVS 读到 WiFi 配置: SSID=\"%s\" (%s)",
             s_ssid, s_pass[0] ? "有密码" : "开放网络");
    return true;
}

/*
 * 把配置写进 NVS。
 *
 * 用 NVS_READWRITE 打开: 键不存在时 nvs_set_str 会自动创建。
 * commit 是原子操作 —— 中途断电不会写出半条配置。
 */
esp_err_t wifi_config_save(const char *ssid, const char *pass)
{
    if (ssid == NULL || ssid[0] == '\0') {
        ESP_LOGE(TAG, "SSID 不能为空");
        return ESP_ERR_INVALID_ARG;
    }
    if (strlen(ssid) > SSID_MAX_LEN) {
        ESP_LOGE(TAG, "SSID 太长 (最多 %d 字节, 实际 %d)", SSID_MAX_LEN, (int)strlen(ssid));
        return ESP_ERR_INVALID_SIZE;
    }
    if (pass != NULL && strlen(pass) > PASS_MAX_LEN) {
        ESP_LOGE(TAG, "密码太长 (最多 %d 字节, 实际 %d)", PASS_MAX_LEN, (int)strlen(pass));
        return ESP_ERR_INVALID_SIZE;
    }

    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open 失败: %s", esp_err_to_name(err));
        return err;
    }

    err = nvs_set_str(h, NVS_KEY_SSID, ssid);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "写 SSID 失败: %s", esp_err_to_name(err));
        nvs_close(h);
        return err;
    }

    if (pass != NULL && pass[0] != '\0') {
        err = nvs_set_str(h, NVS_KEY_PASS, pass);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "写密码失败: %s", esp_err_to_name(err));
            nvs_close(h);
            return err;
        }
    } else {
        /* 开放网络: 把旧密码删掉, 否则会残留上一次的密码 */
        err = nvs_erase_key(h, NVS_KEY_PASS);
        if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
            ESP_LOGE(TAG, "删密码失败: %s", esp_err_to_name(err));
            nvs_close(h);
            return err;
        }
    }

    uint32_t ver = WIFI_CFG_VER;
    err = nvs_set_u32(h, NVS_KEY_VER, ver);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "写版本号失败: %s", esp_err_to_name(err));
        nvs_close(h);
        return err;
    }

    err = nvs_commit(h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "提交失败: %s", esp_err_to_name(err));
        nvs_close(h);
        return err;
    }
    nvs_close(h);

    /* 内存里的副本也更新, 这样不用重连也能看到新配置 */
    strncpy(s_ssid, ssid, SSID_MAX_LEN);
    s_ssid[SSID_MAX_LEN] = '\0';
    strncpy(s_pass, (pass != NULL) ? pass : "", PASS_MAX_LEN);
    s_pass[PASS_MAX_LEN] = '\0';
    s_from_nvs = true;

    ESP_LOGI(TAG, "WiFi 配置已保存: SSID=\"%s\" (%s)",
             s_ssid, s_pass[0] ? "有密码" : "开放网络");
    return ESP_OK;
}

/*
 * 删掉 NVS 里的配置, 恢复用编译期默认值。
 *
 * 用 nvs_erase_all 而不是 nvs_erase_key: 以后 namespace 里加了别的键,
 * 全部清掉最干净 (用户说"忘掉", 就是要彻底忘掉)。
 */
esp_err_t wifi_config_forget(void)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open 失败: %s", esp_err_to_name(err));
        return err;
    }

    err = nvs_erase_all(h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "擦除失败: %s", esp_err_to_name(err));
        nvs_close(h);
        return err;
    }

    err = nvs_commit(h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "提交失败: %s", esp_err_to_name(err));
        nvs_close(h);
        return err;
    }
    nvs_close(h);

    s_from_nvs = false;
    s_ssid[0] = '\0';
    s_pass[0] = '\0';
    ESP_LOGI(TAG, "NVS 里的 WiFi 配置已清除, 下次开机会用编译期默认值");
    return ESP_OK;
}
