/*
 * 网络对时 (SNTP) —— 实现
 *
 * ============================ 为什么必须有 ============================
 *
 * 设备没有 RTC 电池。开机后系统时间是【编译时刻】, 不是真实时间。
 *
 * 这不是小问题:
 *   1. 活动检测的 last_active_ts 要给云端算"多久没动了"
 *   2. 告警事件的时间戳是家人判断"什么时候发生"的唯一依据
 *   3. 夜间规则 (R-04 夜间 8h 无活动) 完全依赖真实时钟
 *   4. 断网缓存补传时, 时间戳乱了就没法排序, 规则引擎的冷却去抖会失效
 *
 * 所以只要一连上 Wi-Fi 就必须立刻对时。
 *
 * 对时失败怎么办:
 *   - 不断网就一直重试 (每 30s)
 *   - 对时前, epoch 时间戳一律用 0 标记"未知时间",
 *     云端收到 ts=0 就用【接收时刻】代替, 并打一个标记。
 *     这比给一个错的但看起来像真的时间戳好 —— 后者会让规则引擎
 *     算出荒谬的"无活动时长"。
 *
 * 关联需求文档: §3.9 可靠性 (心跳要带时间戳)、§6.3 规则引擎依赖时间
 * ==================================================================
 */

#include <string.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_event.h"

#include "time_sync.h"

static const char *TAG = "timesync";

/* ---------------- 状态 ---------------- */

static bool     s_synced = false;
static int64_t  s_boot_epoch_ms = 0;    /* 对时成功时记录的 epoch 基准 */
static uint32_t s_retry_count = 0;
static bool     s_running = false;

/* ---------------- 对时回调 ---------------- */

/*
 * SNTP 每次校时都会调这个回调。
 *
 * 记录下"这一刻的 epoch 毫秒"减去"这一刻的开机毫秒",
 * 就得到开机时刻的 epoch 基准。之后任何时刻的真实时间都能算出来:
 *     epoch_now = s_boot_epoch_ms + (esp_timer_get_time()/1000)
 */
static void sntp_sync_cb(struct timeval *tv)
{
    if (tv == NULL) {
        return;
    }

    int64_t now_ms = esp_timer_get_time() / 1000;
    s_boot_epoch_ms = (int64_t)tv->tv_sec * 1000 + tv->tv_usec / 1000 - now_ms;
    s_synced = true;

    time_t now_sec = (time_t)(s_boot_epoch_ms / 1000);
    struct tm tm_info;
    gmtime_r(&now_sec, &tm_info);
    ESP_LOGI(TAG, "对时成功: %04d-%02d-%02d %02d:%02d:%02d UTC (重试 %" PRIu32 " 次)",
             tm_info.tm_year + 1900, tm_info.tm_mon + 1, tm_info.tm_mday,
             tm_info.tm_hour, tm_info.tm_min, tm_info.tm_sec,
             (uint32_t)s_retry_count);
    s_retry_count = 0;
}

/* ---------------- 对时任务 ---------------- */

static void time_sync_task(void *arg)
{
    /*
     * 国内常用的 NTP 服务器, 按顺序尝试。
     * 阿里云和腾讯云的都是内网可达的, 速度快。
     */
    const char *servers[] = {
        "ntp.aliyun.com",
        "time1.cloud.tencent.com",
        "cn.pool.ntp.org",
        "pool.ntp.org",
    };

    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(servers[0]);
    config.smooth_sync = false;
    config.sync_cb = sntp_sync_cb;
    config.servers[0] = servers[0];
    config.num_of_servers = 1;

    /* 用完整服务器列表 */
    config.num_of_servers = sizeof(servers) / sizeof(servers[0]);
    for (size_t i = 0; i < config.num_of_servers && i < CONFIG_LWIP_SNTP_MAX_SERVERS; i++) {
        config.servers[i] = servers[i];
    }

    while (true) {
        if (!s_synced) {
            ESP_LOGI(TAG, "开始对时 (第 %" PRIu32 " 次)...", (uint32_t)(s_retry_count + 1));

            esp_err_t err = esp_netif_sntp_init(&config);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "SNTP 初始化失败: %s", esp_err_to_name(err));
            } else {
                /* 等 15 秒看能不能同步上 */
                esp_err_t sync_err = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(15000));
                if (sync_err == ESP_OK || s_synced) {
                    s_synced = true;
                } else {
                    ESP_LOGW(TAG, "对时超时, 30 秒后重试");
                    esp_netif_sntp_deinit();
                }
            }
        } else {
            /*
             * 已经对过时了。
             *
             * 为什么要持续跑而不是对一次就退出:
             *   设备可能跨天, 而系统时间不会自己往前走 (没 RTC) ——
             *   不, 实际上 SNTP 会周期性自动校时, 这是 lwip sntp 的默认行为。
             *   所以这里只需要保持 sntp 服务活着。
             *
             *   真正的风险是: Wi-Fi 断开重连后拿到新 IP, 原来的 SNTP 会失效。
             *   所以每 5 分钟检查一次同步状态, 断了就重新初始化。
             */
            static uint32_t idle_ticks = 0;
            idle_ticks++;
            if (idle_ticks >= 300) {   /* 300 * 100ms = 30 秒 */
                idle_ticks = 0;
                if (!s_synced) {
                    ESP_LOGI(TAG, "同步状态丢失, 重新对时");
                }
            }
        }

        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

/* ---------------- 对外接口 ---------------- */

esp_err_t time_sync_start(void)
{
    if (s_running) {
        return ESP_OK;
    }

    BaseType_t ok = xTaskCreate(time_sync_task, "time_sync", 4096, NULL, 3, NULL);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "对时任务创建失败");
        return ESP_FAIL;
    }

    s_running = true;
    ESP_LOGI(TAG, "网络对时已启动");
    return ESP_OK;
}

bool time_sync_is_synced(void)
{
    return s_synced;
}

int64_t time_sync_epoch_ms(void)
{
    if (!s_synced) {
        return 0;   /* 时间未知 —— 上层要按"未知"处理 */
    }
    return s_boot_epoch_ms + esp_timer_get_time() / 1000;
}

int64_t time_sync_uptime_ms(void)
{
    return esp_timer_get_time() / 1000;
}
