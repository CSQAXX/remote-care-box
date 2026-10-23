/*
 * 远程看护盒子 - 可行性验证固件 (M1 摄像头出图 + M2 按键弹话)
 *
 * 硬件: 正点原子 ATK_DNESP32S3_BOX3
 *   - 摄像头: GC0308 (I2C 0x21), DVP 8bit, 无硬件 JPEG
 *   - SCCB 复用共享 I2C: SDA=3 / SCL=2
 *   - 摄像头复位与 2.8V 供电走 AW9523B IO 扩展器 (0x59)
 *   - 摄像头时钟 XCLK 由板载电路提供 (NC)
 *
 * 功能:
 *   - 连办公室 Wi-Fi, 拿到内网 IP
 *   - HTTP 服务器 (8080):
 *       GET  /         带 <img> 的页面, 同时轮询 /event
 *       GET  /stream   MJPEG 实时画面 (RGB565 -> 软件 JPEG 编码)
 *       GET  /event    取一个按键事件 (JSON)
 *       POST /notify   电脑确认收到
 *       GET  /health   状态
 *   - 按板子 BOOT 键 -> 网页弹出中文提示
 *
 * 不做: 云端 / 后端 / 数据库 / 小程序 / 双向对讲
 *
 * 配套文档: docs/可行性验证实施计划.md
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"

#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_psram.h"
#include "esp_http_server.h"
#include "esp_camera.h"
#include "esp_timer.h"

#include "nvs_flash.h"
#include "driver/gpio.h"
#include "driver/i2c.h"

#include "img_converters.h"
#include "camera_pins.h"
#include "aw9523b.h"

static const char *TAG = "kanhu";

#define HTTP_PORT       8080
#define JPEG_QUALITY    12    /* 0-63，越小越清晰 */

/* ---------------- 全局状态 ---------------- */

typedef struct {
    bool         has_event;
    uint32_t     seq;
    char         message[128];
    uint64_t     ts_ms;
} button_event_t;

static QueueHandle_t        s_event_queue;
static EventGroupHandle_t   s_wifi_events;
static esp_netif_t         *s_netif;
static int                 s_stream_clients = 0;
static uint32_t             s_frame_count = 0;

#define WIFI_CONNECTED_BIT BIT0

/* ---------------- Wi-Fi ---------------- */

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
        ESP_LOGI(TAG, "Wi-Fi 启动，开始连接");
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT);
        ESP_LOGW(TAG, "Wi-Fi 断开，尝试重连");
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "已连接 Wi-Fi，内网 IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init(void)
{
    s_wifi_events = xEventGroupCreate();
    s_netif = esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid     = WIFI_SSID,
            .password = WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Wi-Fi 初始化完成");
}

/* ---------------- 共享 I2C (旧版驱动) ---------------- */

/*
 * 摄像头 SCCB 与 AW9523B 共用 I2C0 (SDA=3/SCL=2)。
 * 这里显式用旧版 API 装驱动, 并把 SCCB 指向同一个端口, 避免新旧驱动冲突。
 */
static void board_i2c_init(void)
{
    i2c_config_t conf = {
        .mode                = I2C_MODE_MASTER,
        .sda_io_num         = BOARD_I2C_SDA_PIN,
        .scl_io_num         = BOARD_I2C_SCL_PIN,
        .sda_pullup_en      = GPIO_PULLUP_ENABLE,
        .scl_pullup_en      = GPIO_PULLUP_ENABLE,
        .master.clk_speed    = BOARD_I2C_FREQ_HZ,
    };

    esp_err_t err = i2c_param_config(BOARD_I2C_PORT, &conf);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c_param_config 失败: %s", esp_err_to_name(err));
        return;
    }
    err = i2c_driver_install(BOARD_I2C_PORT, I2C_MODE_MASTER, 0, 0, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c_driver_install 失败: %s", esp_err_to_name(err));
        return;
    }
    ESP_LOGI(TAG, "共享 I2C 初始化完成 (port=%d, SDA=%d, SCL=%d)",
             (int)BOARD_I2C_PORT, BOARD_I2C_SDA_PIN, BOARD_I2C_SCL_PIN);
}

/* 摄像头复位 + 2.8V 供电 (走 AW9523B) */
static void camera_power_reset(void)
{
    if (aw9523b_init(BOARD_I2C_PORT, AW9523B_I2C_ADDR) != ESP_OK) {
        ESP_LOGW(TAG, "AW9523B 不可用，跳过摄像头电源/复位控制");
        return;
    }

    /* XIO14 = 摄像头 2.8V 供电使能, XIO15 = 摄像头复位 */
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_CAM_2V8_EN, true);
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_CAM_RESET, true);

    /* 上电 2.8V */
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_CAM_2V8_EN, true);
    vTaskDelay(pdMS_TO_TICKS(50));

    /* 复位时序: 拉低保持 50ms -> 拉高等待 50ms */
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_CAM_RESET, false);
    vTaskDelay(pdMS_TO_TICKS(50));
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_CAM_RESET, true);
    vTaskDelay(pdMS_TO_TICKS(50));

    ESP_LOGI(TAG, "摄像头供电与复位完成");
}

/* ---------------- 摄像头 ---------------- */

/* GC0308 不支持硬件 JPEG，必须用 RGB565 再软件编码 */
static camera_config_t camera_config = {
    .pin_pwdn  = CAM_PIN_PWDN,
    .pin_reset = CAM_PIN_RESET,
    .pin_xclk  = CAM_PIN_XCLK,
    .pin_sccb_sda = CAM_PIN_SIOD,
    .pin_sccb_scl = CAM_PIN_SIOC,

    .pin_d7 = CAM_PIN_D7,
    .pin_d6 = CAM_PIN_D6,
    .pin_d5 = CAM_PIN_D5,
    .pin_d4 = CAM_PIN_D4,
    .pin_d3 = CAM_PIN_D3,
    .pin_d2 = CAM_PIN_D2,
    .pin_d1 = CAM_PIN_D1,
    .pin_d0 = CAM_PIN_D0,
    .pin_vsync = CAM_PIN_VSYNC,
    .pin_href  = CAM_PIN_HREF,
    .pin_pclk  = CAM_PIN_PCLK,

    .xclk_freq_hz = 10000000,
    .ledc_timer   = LEDC_TIMER_0,
    .ledc_channel = LEDC_CHANNEL_0,

    .pixel_format = PIXFORMAT_RGB565,
    .frame_size   = FRAMESIZE_QVGA,   /* 320x240 */
    .fb_count     = 2,
    .fb_location  = CAMERA_FB_IN_PSRAM,
    .grab_mode    = CAMERA_GRAB_WHEN_EMPTY,

    /* SCCB 复用共享 I2C 总线 (SDA=3/SCL=2) */
    .sccb_i2c_port = BOARD_I2C_PORT,
};

static void camera_init(void)
{
    esp_err_t err = esp_camera_init(&camera_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "摄像头初始化失败: 0x%x", err);
        ESP_LOGE(TAG, "  排查: 1)PSRAM Octal 2)SCCB 复用 I2C 是否已初始化 3)GC0308 供电/复位");
        return;
    }

    sensor_t *s = esp_camera_sensor_get();
    if (s != NULL) {
        ESP_LOGI(TAG, "摄像头型号 PID: 0x%04x (期望 GC0308 = 0x9a)", s->id.PID);
        s->set_vflip(s, 1);
        s->set_brightness(s, 1);
    }
    ESP_LOGI(TAG, "摄像头初始化成功");
}

/* ---------------- HTTP 处理器 ---------------- */

static esp_err_t root_handler(httpd_req_t *req)
{
    const char html[] =
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>远程看护盒子</title>"
        "<style>body{font-family:sans-serif;margin:20px;background:#fafafa}"
        "img{border:2px solid #333;max-width:100%;background:#000}"
        "#alert{display:none;background:#ffe0e0;border:2px solid #d00;padding:16px;"
        "margin-top:16px;font-size:20px;font-weight:bold}"
        "</style></head><body>"
        "<h2>远程看护盒子（可行性验证）</h2>"
        "<img id=\"cam\" src=\"/stream\" width=\"320\" height=\"240\">"
        "<div id=\"alert\"></div>"
        "<script>"
        "function poll(){fetch('/event').then(r=>r.json()).then(d=>{"
        "if(d.type==='button'){var a=document.getElementById('alert');"
        "a.textContent=d.msg;a.style.display='block';"
        "fetch('/notify',{method:'POST',body:d.msg});"
        "setTimeout(function(){a.style.display='none';},8000);}}"
        "}).catch(function(e){});}"
        "setInterval(poll,500);poll();"
        "</script></body></html>";

    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send(req, html, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/* MJPEG 流的分块发送器 */
static esp_err_t stream_handler(httpd_req_t *req)
{
    s_stream_clients++;
    ESP_LOGI(TAG, "画面客户端接入，当前 %d 个", s_stream_clients);

    const char *headers =
        "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
        "Cache-Control: no-cache, no-store, must-revalidate\r\n"
        "Pragma: no-cache\r\n"
        "Connection: close\r\n\r\n";

    esp_err_t res = httpd_resp_send(req, headers, HTTPD_RESP_USE_STRLEN);
    if (res != ESP_OK) {
        goto cleanup;
    }

    while (true) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb == NULL) {
            ESP_LOGW(TAG, "取帧失败");
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        /* GC0308 只有 RGB565，需要软件转 JPEG */
        uint8_t *jpg_buf = NULL;
        size_t jpg_len = 0;
        if (!frame2jpg(fb, JPEG_QUALITY, &jpg_buf, &jpg_len) || jpg_buf == NULL) {
            esp_camera_fb_return(fb);
            ESP_LOGW(TAG, "JPEG 编码失败");
            continue;
        }

        char part_buf[96];
        int len = snprintf(part_buf, sizeof(part_buf),
                           "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                           (unsigned int)jpg_len);
        bool ok = (httpd_resp_send_chunk(req, part_buf, len) == ESP_OK);

        if (ok) {
            ok = (httpd_resp_send_chunk(req, (const char *)jpg_buf, jpg_len) == ESP_OK);
        }
        if (ok) {
            ok = (httpd_resp_send_chunk(req, "\r\n", 2) == ESP_OK);
        }

        free(jpg_buf);
        esp_camera_fb_return(fb);

        if (!ok) {
            break;
        }
        s_frame_count++;
    }

cleanup:
    s_stream_clients--;
    ESP_LOGI(TAG, "画面客户端断开，剩余 %d 个", s_stream_clients);
    return ESP_OK;
}

static esp_err_t event_handler(httpd_req_t *req)
{
    char resp[256];

    button_event_t ev;
    if (xQueueReceive(s_event_queue, &ev, 0) == pdTRUE) {
        snprintf(resp, sizeof(resp),
                 "{\"type\":\"button\",\"msg\":\"%s\",\"seq\":%u,\"ts\":%llu}",
                 ev.message, (unsigned int)ev.seq, (unsigned long long)ev.ts_ms);
        ESP_LOGI(TAG, "取出一个按键事件: %s", ev.message);
    } else {
        snprintf(resp, sizeof(resp), "{\"type\":\"none\"}");
    }

    httpd_resp_set_type(req, "application/json; charset=utf-8");
    httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

static esp_err_t notify_handler(httpd_req_t *req)
{
    char buf[256];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) {
        len = 0;
    }
    buf[len] = '\0';
    ESP_LOGI(TAG, "电脑已确认收到: %s", buf);

    httpd_resp_set_type(req, "application/json");
    httpd_resp_send(req, "{\"ok\":true}", HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

static esp_err_t health_handler(httpd_req_t *req)
{
    esp_netif_ip_info_t ip;
    esp_netif_get_ip_info(s_netif, &ip);

    char resp[256];
    snprintf(resp, sizeof(resp),
             "{\"ip\":\"" IPSTR "\",\"uptime\":%llu,\"frames\":%u,\"clients\":%d,\"psram\":%u}",
             IP2STR(&ip.ip),
             (unsigned long long)(esp_timer_get_time() / 1000),
             (unsigned int)s_frame_count,
             s_stream_clients,
             (unsigned int)esp_psram_get_size());

    httpd_resp_set_type(req, "application/json");
    httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

static void http_server_start(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = HTTP_PORT;
    config.max_uri_handlers = 8;
    config.stack_size = 10240;

    httpd_handle_t server = NULL;
    esp_err_t ret = httpd_start(&server, &config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "HTTP 服务器启动失败: %s", esp_err_to_name(ret));
        return;
    }

    httpd_uri_t root = { .uri = "/", .method = HTTP_GET, .handler = root_handler, .user_ctx = NULL };
    httpd_uri_t stream = { .uri = "/stream", .method = HTTP_GET, .handler = stream_handler, .user_ctx = NULL };
    httpd_uri_t ev = { .uri = "/event", .method = HTTP_GET, .handler = event_handler, .user_ctx = NULL };
    httpd_uri_t notify = { .uri = "/notify", .method = HTTP_POST, .handler = notify_handler, .user_ctx = NULL };
    httpd_uri_t health = { .uri = "/health", .method = HTTP_GET, .handler = health_handler, .user_ctx = NULL };

    httpd_register_uri_handler(server, &root);
    httpd_register_uri_handler(server, &stream);
    httpd_register_uri_handler(server, &ev);
    httpd_register_uri_handler(server, &notify);
    httpd_register_uri_handler(server, &health);

    ESP_LOGI(TAG, "HTTP 服务器已启动，端口 %d", HTTP_PORT);
}

/* ---------------- 按键任务 ---------------- */

/*
 * 触发源:
 *   1) BOOT 键 (GPIO0, 板载直连)
 *   2) K1 / K2 键 (AW9523B 扩展器上的 XIO0 / XIO1, 低有效)
 * 任一按下都会产生一个事件, 网页弹出提示语。
 */
static bool aw_key_pressed(void)
{
    bool high = true;

    /* XIO0 / XIO1 配置成输入 */
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_KEY_K1, false);
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_KEY_K2, false);

    if (aw9523b_get_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_KEY_K1, &high) == ESP_OK) {
        if (!high) {
            return true;
        }
    }
    if (aw9523b_get_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_KEY_K2, &high) == ESP_OK) {
        if (!high) {
            return true;
        }
    }
    return false;
}

static void button_task(void *arg)
{
    uint32_t last_event_ms = 0;

    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << BTN_BOOT_GPIO),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    ESP_LOGI(TAG, "按键已配置 (BOOT=GPIO%d, K1/K2=AW9523B XIO%d/XIO%d)",
             BTN_BOOT_GPIO, XIO_KEY_K1, XIO_KEY_K2);

    while (true) {
        bool boot_down = (gpio_get_level(BTN_BOOT_GPIO) == 0);
        bool key_down  = aw_key_pressed();

        if (boot_down || key_down) {
            vTaskDelay(pdMS_TO_TICKS(30));

            /* 等到松开 */
            while (gpio_get_level(BTN_BOOT_GPIO) == 0 || aw_key_pressed()) {
                vTaskDelay(pdMS_TO_TICKS(20));
            }

            uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
            if (now - last_event_ms < 1000) {
                continue;
            }
            last_event_ms = now;

            button_event_t ev;
            memset(&ev, 0, sizeof(ev));
            ev.has_event = true;
            ev.seq = now;
            ev.ts_ms = esp_timer_get_time() / 1000;
            snprintf(ev.message, sizeof(ev.message), "%s", BUTTON_MESSAGE);

            ESP_LOGI(TAG, "检测到按键按下，生成事件: %s", ev.message);
            xQueueSend(s_event_queue, &ev, 0);
        }

        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

/* ---------------- 主函数 ---------------- */

void app_main(void)
{
    printf("\n========================================\n");
    printf("  远程看护盒子 - M1/M2 可行性验证固件\n");
    printf("========================================\n");

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    s_event_queue = xQueueCreate(8, sizeof(button_event_t));
    if (s_event_queue == NULL) {
        ESP_LOGE(TAG, "事件队列创建失败");
        return;
    }

    ESP_LOGI(TAG, "PSRAM: %u bytes", (unsigned int)esp_psram_get_size());

    board_i2c_init();
    camera_power_reset();
    camera_init();

    wifi_init();

    ESP_LOGI(TAG, "等待 Wi-Fi 连接...");
    EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED_BIT,
                                           pdFALSE, pdTRUE, pdMS_TO_TICKS(30000));
    if (bits & WIFI_CONNECTED_BIT) {
        esp_netif_ip_info_t ip;
        esp_netif_get_ip_info(s_netif, &ip);
        ESP_LOGI(TAG, "========================================");
        ESP_LOGI(TAG, "  浏览器打开: http://" IPSTR ":%d/", IP2STR(&ip.ip), HTTP_PORT);
        ESP_LOGI(TAG, "  画面地址:   http://" IPSTR ":%d/stream", IP2STR(&ip.ip), HTTP_PORT);
        ESP_LOGI(TAG, "========================================");
    } else {
        ESP_LOGE(TAG, "Wi-Fi 连接超时！请检查 camera_pins.h 里的 SSID/密码");
    }

    http_server_start();

    xTaskCreate(button_task, "button_task", 3072, NULL, 5, NULL);

    printf("\nM1/M2 固件启动完成\n\n");
}
