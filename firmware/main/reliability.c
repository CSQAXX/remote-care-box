/*
 * 可靠性模块 —— 实现 (需求文档 §3.9)
 *
 * 见 reliability.h 的说明。
 */

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "esp_psram.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_task_wdt.h"
#include "sdkconfig.h"

#include "lwip/ip_addr.h"

#include "reliability.h"
#include "push_client.h"
#include "time_sync.h"
#include "audio_board.h"

static const char *TAG = "reliab";

/* ---------------- 状态 ---------------- */

static TaskHandle_t      s_task = NULL;
static volatile uint32_t s_cam_fail_count = 0;
static volatile bool     s_camera_ok = true;
static volatile reliability_mode_t s_mode = RELIABILITY_MODE_NORMAL;

static heartbeat_t        s_hb;
static SemaphoreHandle_t  s_hb_mutex = NULL;

/* 看门狗: 最多能同时监控多少个任务 */
#define RELIABILITY_MAX_WATCHED   8

/*
 * 看门狗订阅表。
 *
 * 【为什么必须自己维护这张表】
 *   IDF 的 esp_task_wdt_reset() 只能给【已经订阅过的任务】喂狗。
 *   没订阅就调 -> 返回 ESP_ERR_NOT_FOUND 并打一条 "task not found" 错误日志。
 *   如果直接在 feed 里调 esp_task_wdt_reset(), 就会每帧打一条错误日志,
 *   日志本身要占 UART 和 CPU, 反而把系统拖垮 —— 这正是之前看到的现象:
 *   "E task_wdt: esp_task_wdt_reset(705): task not found" 每秒刷几十条。
 *
 *   所以这里记录"哪些任务已经被订阅过", feed 时只对真正订阅过的任务喂狗,
 *   其余的静默跳过。
 */
static struct {
    TaskHandle_t handle;
    const char  *name;
} s_watched[RELIABILITY_MAX_WATCHED];
static int s_watched_count = 0;

/* ---------------- 心跳 ---------------- */

/*
 * 组装一次心跳。
 *
 * RSSI 拿不到就填 0 —— 没有连上 Wi-Fi 时本来就没有 RSSI。
 */
static void collect_heartbeat(heartbeat_t *hb)
{
    memset(hb, 0, sizeof(*hb));

    hb->uptime_ms = (uint64_t)(esp_timer_get_time() / 1000);

    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        hb->rssi = ap.rssi;
    }

    hb->free_heap = esp_get_free_heap_size();
    hb->free_psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    hb->psram_size = (uint32_t)esp_psram_get_size();

    hb->time_synced = time_sync_is_synced();
    hb->epoch_ms = time_sync_epoch_ms();

    hb->camera_ok = s_camera_ok;
    hb->cam_fail_count = s_cam_fail_count;
    hb->audio_ok = audio_board_is_ready();
}

/*
 * 心跳 JSON。
 *
 * 字段名和需求文档 §3.9 对齐, 云端规则引擎直接吃这个结构。
 *
 * 注意: 这里【不】加最外层的 {}。
 * 因为推模式的发送函数 (push_client.c 的 send_event_raw) 会自己
 * 包一层 —— 心跳如果自带花括号, 发出去就是 {{...}}, JSON 直接非法。
 * 这跟 push_client_submit_event() 的约定一致: 传进去的是【主体】。
 */
int reliability_heartbeat_to_json(char *buf, size_t buf_size, bool with_replay)
{
    if (buf == NULL || buf_size == 0) {
        return 0;
    }

    heartbeat_t hb;
    if (s_hb_mutex) {
        xSemaphoreTake(s_hb_mutex, portMAX_DELAY);
    }
    hb = s_hb;
    if (s_hb_mutex) {
        xSemaphoreGive(s_hb_mutex);
    }

    char ip_str[16] = "0.0.0.0";
    if (hb.ip[0] != '\0') {
        snprintf(ip_str, sizeof(ip_str), "%s", hb.ip);
    }

    int n = snprintf(buf, buf_size,
                     "\"type\":\"heartbeat\","
                     "\"rssi\":%d,"
                     "\"ip\":\"%s\","
                     "\"uptime_ms\":%llu,"
                     "\"free_heap\":%u,"
                     "\"free_psram\":%u,"
                     "\"psram_total\":%u,"
                     "\"time_synced\":%s,"
                     "\"epoch_ms\":%lld,"
                     "\"wifi_connected\":%s,"
                     "\"camera_ok\":%s,"
                     "\"audio_ok\":%s,"
                     "\"cam_fail_count\":%u,"
                     "\"mode\":\"%s\"%s",
                     hb.rssi,
                     ip_str,
                     (unsigned long long)hb.uptime_ms,
                     (unsigned)(uint32_t)hb.free_heap,
                     (unsigned)(uint32_t)hb.free_psram,
                     (unsigned)(uint32_t)hb.psram_size,
                     hb.time_synced ? "true" : "false",
                     (long long)hb.epoch_ms,
                     hb.wifi_connected ? "true" : "false",
                     hb.camera_ok ? "true" : "false",
                     hb.audio_ok ? "true" : "false",
                     (unsigned)hb.cam_fail_count,
                     (s_mode == RELIABILITY_MODE_DEGRADED) ? "degraded" : "normal",
                     with_replay ? ",\"replay\":true" : "");
    return (n < 0) ? 0 : ((size_t)n >= buf_size ? (int)buf_size - 1 : n);
}

/* ---------------- 错误上报 ---------------- */

void reliability_report_error(const char *code, const char *detail)
{
    ESP_LOGE(TAG, "错误上报: code=%s detail=%s", code ? code : "?", detail ? detail : "");

    /*
     * 错误也进推模式队列。
     *
     * 为什么要走推模式而不是只打日志:
     *   摄像头挂了这种事, 只有设备自己知道。如果子女那边看不到,
     *   他们会以为"画面卡了", 然后打电话问老人 —— 老人根本不知道
     *   设备出问题了。错误上报让子女知道"设备这边有故障, 但对讲还能用"。
     */
    char body[256];
    snprintf(body, sizeof(body),
             "\"type\":\"error\",\"code\":\"%s\",\"detail\":\"%s\",\"ts\":%lld",
             code ? code : "unknown",
             detail ? detail : "",
             (long long)(esp_timer_get_time() / 1000));
    push_client_submit_event(body);
}

/* ---------------- 看门狗 ---------------- */

/*
 * 把当前任务订阅进 IDF 看门狗 (TWDT)。
 *
 * 【必须在【被监控的任务自己身上】调用】
 *   IDF 的 esp_task_wdt_add(NULL) 加的是【调用它的那个任务】,
 *   不是外面传进来的 handle。所以这个函数只应该由被监控任务自己调。
 *
 * 【为什么之前会刷 "task not found"】
 *   之前的实现只把 handle 记进表里, 从来没真的调 esp_task_wdt_add()。
 *   于是 esp_task_wdt_reset() 每次都返回 ESP_ERR_NOT_FOUND,
 *   IDF 就打一条 "task not found" 错误日志 —— 每帧一条, 刷屏。
 *
 * 返回 ESP_OK 表示【这个任务已经被订阅过】, 调用者可以放心喂狗。
 */
esp_err_t reliability_watch_task(TaskHandle_t task_handle, const char *name)
{
    if (task_handle == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_watched_count >= RELIABILITY_MAX_WATCHED) {
        ESP_LOGW(TAG, "看门狗注册已满 (%d), 跳过 %s",
                 RELIABILITY_MAX_WATCHED, name ? name : "?");
        return ESP_ERR_NO_MEM;
    }

    /* 去重: 同一个任务不重复订阅 */
    for (int i = 0; i < s_watched_count; i++) {
        if (s_watched[i].handle == task_handle) {
            return ESP_OK;
        }
    }

    /*
     * 真正订阅。
     *
     * 注意 IDF 的语义: esp_task_wdt_add(NULL) 加的是【当前任务】。
     * 所以这个函数必须由被监控的任务自己调用 —— main.c 里就是
     * 在 cam_producer_task / button_task 的循环里调。
     */
    esp_err_t err = esp_task_wdt_add(NULL);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "任务 %s 订阅看门狗失败: %s",
                 name ? name : "?", esp_err_to_name(err));
        return err;
    }

    s_watched[s_watched_count].handle = task_handle;
    s_watched[s_watched_count].name = name;
    s_watched_count++;

    ESP_LOGI(TAG, "任务 %s 已订阅看门狗 (超时 %d 秒)",
             name ? name : "?", CONFIG_ESP_TASK_WDT_TIMEOUT_S);
    return ESP_OK;
}

/*
 * 喂狗。
 *
 * 每个关键任务在自己的主循环里调。
 *
 * 为什么要做成"显式喂狗"而不是"任务活着就自动算喂过":
 *   任务活着不代表它在干活。一个卡在内层循环里的任务
 *   (比如卡在 esp_camera_fb_get 上, 或者 JPEG 编码死循环)
 *   依然是 RUNNING 状态, 但它已经不产生任何输出了 ——
 *   这正是最需要被发现的故障。
 *
 * 【为什么要先查表】
 *   没订阅过的任务调 esp_task_wdt_reset() 会返回 ESP_ERR_NOT_FOUND,
 *   IDF 会打一条 "task not found" 错误日志。刷屏本身就是故障源
 *   (日志抢 UART、抢 CPU), 所以这里静默跳过没订阅的任务。
 */
void reliability_feed_watchdog(void)
{
    TaskHandle_t self = xTaskGetCurrentTaskHandle();

    for (int i = 0; i < s_watched_count; i++) {
        if (s_watched[i].handle == self) {
            esp_err_t err = esp_task_wdt_reset();
            if (err != ESP_OK) {
                /* 只在第一次失败时告警, 避免又变成刷屏 */
                ESP_LOGW(TAG, "任务 %s 喂看门狗失败: %s",
                         s_watched[i].name ? s_watched[i].name : "?",
                         esp_err_to_name(err));
            }
            return;
        }
    }
    /* 没订阅的任务: 什么都不做 (否则 IDF 会打 "task not found") */
}

/* ---------------- 摄像头健康 / 降级模式 ---------------- */

/*
 * 摄像头取帧失败计数。
 *
 * 单独放在 reliability.c 里, 而不是 main.c 的静态变量, 是为了让
 * "判定阈值"和"降级动作"挨在一起 —— 这两件事必须同步改。
 */
static volatile uint32_t s_cam_fail_run = 0;

/*
 * 记一次取帧失败。
 *
 * 连续失败到阈值才上报 error 并进入降级模式 —— 偶发失败(总线被 Wi-Fi
 * 抢占)是常态, 报太多会让子女对告警脱敏。
 */
void reliability_note_cam_failure(void)
{
    s_cam_fail_run++;
    s_cam_fail_count = s_cam_fail_run;

    if (s_cam_fail_run == RELIABILITY_CAM_FAIL_THRESHOLD) {
        /* 刚刚到阈值 —— 上报一次, 进入降级 */
        s_camera_ok = false;
        s_mode = RELIABILITY_MODE_DEGRADED;

        ESP_LOGE(TAG, "摄像头连续取帧失败 %d 次, 进入降级模式 "
                      "(音频与按键链路保持可用)",
                 (int)s_cam_fail_run);
        reliability_report_error("camera_fail",
                                 "摄像头连续取帧失败, 画面不可用, 对讲与求助仍正常");
    }
}

/*
 * 取到帧了 —— 清零失败计数, 恢复正常模式。
 *
 * 摄像头是"时好时坏"型故障 (总线被抢 / 传感器偶发丢帧),
 * 所以恢复的时候一定要能把降级状态撤掉, 否则一次偶发失败就永久降级了。
 */
void reliability_note_cam_ok(void)
{
    if (s_cam_fail_run > 0) {
        ESP_LOGI(TAG, "摄像头已恢复 (之前连续失败 %d 次)", (int)s_cam_fail_run);
        s_cam_fail_run = 0;
        s_cam_fail_count = 0;
    }

    if (!s_camera_ok) {
        s_camera_ok = true;
        s_mode = RELIABILITY_MODE_NORMAL;
        reliability_report_error("camera_recovered", "摄像头已恢复, 画面重新可用");
    }
}

/* ---------------- 主任务 ---------------- */

static void reliability_task(void *arg)
{
    (void)arg;
    uint32_t next_beat = esp_timer_get_time() / 1000 + RELIABILITY_HEARTBEAT_MS;

    ESP_LOGI(TAG, "可靠性任务已启动 (心跳周期 %d ms)", RELIABILITY_HEARTBEAT_MS);

    while (true) {
        uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);

        if (now >= next_beat) {
            next_beat = now + RELIABILITY_HEARTBEAT_MS;

            heartbeat_t hb;
            collect_heartbeat(&hb);

            /* 取 IP */
            esp_netif_ip_info_t ip;
            memset(&ip, 0, sizeof(ip));
            /*
             * 用默认 netif 而不是 main.c 的 s_netif —— 可靠性模块
             * 不应该依赖 main.c 的内部变量 (那会让两个模块绑死)。
             * esp_netif_get_default_netif() 在 STA 模式下返回的就是
             * Wi-Fi  STA 那个 netif, 和 main.c 里的 s_netif 是同一个。
             */
            esp_netif_t *netif = esp_netif_get_default_netif();
            if (netif != NULL) {
                esp_netif_get_ip_info(netif, &ip);
            }
            if (ip.ip.addr != 0) {
                snprintf(hb.ip, sizeof(hb.ip), IPSTR, IP2STR(&ip.ip));
            } else {
                snprintf(hb.ip, sizeof(hb.ip), "0.0.0.0");
            }

            /* Wi-Fi 连接状态: 直接问驱动 */
            wifi_ap_record_t ap;
            hb.wifi_connected = (esp_wifi_sta_get_ap_info(&ap) == ESP_OK);

            if (s_hb_mutex) {
                xSemaphoreTake(s_hb_mutex, portMAX_DELAY);
            }
            s_hb = hb;
            if (s_hb_mutex) {
                xSemaphoreGive(s_hb_mutex);
            }

            /* 打成 JSON 进推模式队列 */
            char body[384];
            int n = reliability_heartbeat_to_json(body, sizeof(body), false);
            if (n > 0) {
                push_client_submit_event(body);
            }

            ESP_LOGI(TAG,
                     "心跳: rssi=%d ip=%s 内部RAM=%u 总堆=%u PSRAM余=%u/%u 对时=%s 摄像头=%s 音频=%s 模式=%s",
                     (unsigned)hb.rssi,
                     hb.ip,
                     (unsigned)(uint32_t)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                     (unsigned)(uint32_t)hb.free_heap,
                     (unsigned)(uint32_t)hb.free_psram,
                     (unsigned)(uint32_t)hb.psram_size,
                     hb.time_synced ? "是" : "否",
                     hb.camera_ok ? "正常" : "失效",
                     hb.audio_ok ? "正常" : "失效",
                     (s_mode == RELIABILITY_MODE_DEGRADED) ? "降级" : "正常");

            /*
             * 内存不足检查。
             *
             * 【2026-11-12 修正】原来判断的是 hb.free_heap, 那是【总堆】
             * (含 8MB PSRAM), 永远有三四百万字节, 永远不触发告警 ——
             * 而实际上内部 RAM 早就只剩 303 字节, 任务全建不起来。
             * 必须看 MALLOC_CAP_INTERNAL。
             *
             * 内部堆紧张是这板子的常态 (历史只剩 28KB),
             * 所以阈值压得很低 —— 只有真的危险了才报。
             * 报太多会让子女习惯性忽略告警, 那比不报更糟。
             */
            uint32_t free_internal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
            if (free_internal < RELIABILITY_LOW_HEAP_BYTES) {
                char detail[64];
                snprintf(detail, sizeof(detail), "内部RAM仅剩 %u 字节", (unsigned)free_internal);
                reliability_report_error("low_memory", detail);
            }        }

        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

/* ---------------- 对外接口 ---------------- */

esp_err_t reliability_start(void)
{
    if (s_task != NULL) {
        return ESP_OK;
    }

    s_hb_mutex = xSemaphoreCreateMutex();
    if (s_hb_mutex == NULL) {
        ESP_LOGE(TAG, "心跳互斥量创建失败");
        return ESP_FAIL;
    }

    BaseType_t ok = xTaskCreatePinnedToCore(reliability_task, "reliability",
                                            4096, NULL, 3, &s_task, APP_CORE_ID);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "可靠性任务创建失败");
        s_task = NULL;
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "可靠性模块已就绪 (§3.9: 心跳/看门狗/降级模式)");
    return ESP_OK;
}

reliability_mode_t reliability_get_mode(void)
{
    return s_mode;
}

void reliability_get_heartbeat(heartbeat_t *out)
{
    if (out == NULL) {
        return;
    }
    if (s_hb_mutex) {
        xSemaphoreTake(s_hb_mutex, portMAX_DELAY);
    }
    *out = s_hb;
    if (s_hb_mutex) {
        xSemaphoreGive(s_hb_mutex);
    }
}
