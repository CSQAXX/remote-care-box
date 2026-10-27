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
#include <unistd.h>
#include <inttypes.h>
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
#include "esp_heap_caps.h"
#include "esp_http_server.h"
#include "esp_camera.h"
#include "esp_timer.h"

#include "nvs_flash.h"
#include "driver/gpio.h"
#include "driver/i2c.h"

#include "img_converters.h"
#include "camera_pins.h"
#include "aw9523b.h"
#include "lcd_st7789.h"

static const char *TAG = "kanhu";

#define HTTP_PORT       8080

/*
 * JPEG 质量: 1-100, 越大越清晰 (jpge / libjpeg 的语义)。
 *
 * 之前这里写的 "0-63, 越小越清晰" 是 OpenCV 的约定, 用在这里是错的:
 * 传 12 其实是【最低质量】, 画面被压得又糊又小块。
 * 看护场景要能分清人有没有摔倒/在不在床上, 建议 60-75。
 * 质量越高 -> JPEG 越大 -> 编码越慢 -> 帧率越低, 需要和帧率一起权衡。
 */
#define JPEG_QUALITY    65

/* 帧率策略: LCD 本地预览降频, 把 CPU 让给远程视频流 */
#define LCD_PREVIEW_MS   200   /* LCD 预览间隔 (约 2 fps) */
#define STREAM_BUSY_MSG  "stream busy"

/* ---------------- 自检模式 (定位花屏用) ----------------
 *
 * 0 = 正常模式 (摄像头 + LCD + WiFi + HTTP)
 * 1 = LCD 自检模式: 只初始化 LCD 并反复画测试图样,
 *     不启动摄像头 / 不启动 WiFi / 不开 HTTP。
 * 2 = 合成图模式: 只初始化 LCD, 用【程序生成的 PSRAM 图像】反复走
 *     lcd_draw_rgb565 (和摄像头完全相同的代码路径), 但不接摄像头。
 * 3 = 摄像头诊断模式: 启动摄像头, 用串口输出帧统计 + ASCII 缩略图,
 *     并把原始帧的十六进制导出来 (主机端可还原成 PNG 直接看)。
 *     关键: 绘制【前】和绘制【后】各算一次校验和, 若不一致,
 *     说明帧缓冲在绘制过程中被摄像头 DMA 改写了 —— 这就是花屏的根因。
 */
#define LCD_SELFTEST_ONLY   0

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
static volatile int         s_stream_clients = 0;
static uint32_t             s_frame_count = 0;

/* ---------------- 帧槽位 (生产者 / 消费者) ---------------- */

/* 前置声明: 编码函数定义在生产者任务之后 */
static size_t encode_jpeg_to_slot(const uint8_t *rgb565, int w, int h, uint8_t *dst);

/*
 * 摄像头只有一个, 但有两个消费者: LCD 本地预览 + 远程 MJPEG 视频流。
 *
 * 之前的写法是两边各自调 esp_camera_fb_get():
 *   1) 互相抢帧 —— 谁抢到谁用, 另一边要么等, 要么拿到【正在被 DMA 改写】的帧
 *      (fb_count=2 时会撞车, 表现就是花屏)
 *   2) LCD 那路每 100ms 做一次整屏拷贝 + 逐像素字节交换, 把 CPU 抢光,
 *      视频流自然就慢了
 *
 * 现在改成【生产者 - 消费者】:
 *   cam_task  : 唯一碰摄像头的任务, 取帧 -> 拷进 RGB565 槽位 -> 编码 JPEG -> 投递
 *   LCD 任务   : 只从 RGB565 槽位取现成的帧, 不碰摄像头
 *   stream HTTP: 只从 JPEG 槽位取现成的帧, 不碰摄像头
 *
 * 槽位用 2 个 + busy/ready 状态位, 保证生产者和消费者不会同时写同一块缓冲。
 *
 * 附带好处: 拷进自己槽位之后, LCD 画的是【稳定副本】, 不会再被摄像头 DMA
 * 改写, 花屏问题从根上消失 (不用再靠 fb_count=2 赌运气)。
 */
#define SLOT_COUNT          2
#define RGB565_SLOT_BYTES   (320 * 240 * 2)
#define JPEG_SLOT_BYTES     (64 * 1024)

typedef struct {
    uint8_t *buf;
    size_t   len;
    bool     ready;   /* 有完整数据等待被取走 */
    bool     busy;    /* 正在被写 或 正在被读 */
} frame_slot_t;

static frame_slot_t   s_rgb_slots[SLOT_COUNT];
static frame_slot_t   s_jpg_slots[SLOT_COUNT];
static SemaphoreHandle_t s_slot_mutex = NULL;

/*
 * 四象限诊断用的内部 RAM 副本缓冲 (160x120x2 = 38400 字节)。
 * 必须放内部 RAM: 诊断的目的就是对比 "PSRAM 源" 和 "内部 RAM 副本",
 * 如果副本也在 PSRAM 就失去对比意义了。
 */
#define DIAG_W 160
#define DIAG_H 120
static uint8_t *s_diag_copy_buf = NULL;

/* 统计信息 (给 /health 看) */
static volatile uint32_t s_cam_frames = 0;      /* 摄像头取帧数 */
static volatile uint32_t s_jpg_frames = 0;      /* JPEG 编码成功数 */
static volatile uint32_t s_jpg_bytes = 0;       /* 最近一帧 JPEG 大小 */
static volatile uint32_t s_jpg_encode_us = 0;   /* 最近一帧 JPEG 编码耗时 */
static volatile uint32_t s_cam_get_us = 0;      /* 最近一次取帧耗时 */
static volatile uint32_t s_rgb_copy_us = 0;     /* 最近一次 RGB565 拷贝耗时 */
static volatile uint32_t s_torn_frames = 0;     /* 拷贝过程中被 DMA 改写的帧数 */
static volatile uint32_t s_clean_frames = 0;    /* 拷贝期间保持完好的帧数 */

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_RECONNECT_BIT  BIT1

/* Wi-Fi 重连退避状态 */
static volatile uint32_t s_wifi_retry_count = 0;
static volatile bool     s_wifi_reconnect_pending = false;
static volatile uint32_t s_wifi_reconnect_delay_ms = 0;

/* ---------------- 槽位管理 ---------------- */

/*
 * 槽位策略: 【最新帧优先】(latest-wins)
 *
 * 为什么要丢弃旧帧:
 *   直播场景下, 一帧晚到 100ms 就已经没有价值了 (人已经走了 / 画面已变)。
 *   如果排队等空槽位, 延迟会越积越多, 最后变成"看到的是 3 秒前的画面"。
 *   所以消费者宁可丢掉旧帧, 也要立刻拿到最新的。
 *
 * 槽位分配规则:
 *   生产者: 找一个既不 busy 也不 ready 的空槽 -> 写入 -> publish (ready=true)
 *           如果没有空槽 (说明消费者还没取走上一帧), 强行抢一个 ready 的旧槽,
 *           直接覆盖 —— 旧帧反正会被丢掉。
 *   消费者: 找一个 ready 的槽 -> 拿走 -> release
 */

/* 生产者申请一个可写的槽位 (标记为 busy), 拿到后往里写 */
static bool slot_acquire(frame_slot_t *slots, int *idx)
{
    bool got = false;
    if (xSemaphoreTake(s_slot_mutex, portMAX_DELAY) != pdTRUE) {
        return false;
    }
    /* 优先找完全空的槽位 */
    for (int i = 0; i < SLOT_COUNT; i++) {
        if (!slots[i].busy && !slots[i].ready) {
            slots[i].busy = true;
            *idx = i;
            got = true;
            break;
        }
    }
    /* 没有空槽 -> 抢一个 ready 的旧槽 (消费者还没取走的旧帧, 直接覆盖) */
    if (!got) {
        for (int i = 0; i < SLOT_COUNT; i++) {
            if (slots[i].ready && !slots[i].busy) {
                slots[i].busy  = true;
                slots[i].ready = false;   /* 旧帧作废 */
                *idx = i;
                got = true;
                break;
            }
        }
    }
    xSemaphoreGive(s_slot_mutex);
    return got;
}

/* 生产者写完了 -> 标记 ready, 放开给消费者 */
static void slot_publish(frame_slot_t *slots, int idx)
{
    xSemaphoreTake(s_slot_mutex, portMAX_DELAY);
    slots[idx].ready = true;
    slots[idx].busy  = false;
    xSemaphoreGive(s_slot_mutex);
}

/* 消费者取一个 ready 的槽位 (标记 busy), 取出后必须调 slot_release */
static bool slot_take_ready(frame_slot_t *slots, int *idx, size_t *len)
{
    bool got = false;
    xSemaphoreTake(s_slot_mutex, portMAX_DELAY);
    for (int i = 0; i < SLOT_COUNT; i++) {
        if (slots[i].ready && !slots[i].busy) {
            slots[i].busy = true;
            *idx = i;
            if (len) {
                *len = slots[i].len;
            }
            got = true;
            break;
        }
    }
    xSemaphoreGive(s_slot_mutex);
    return got;
}

/* 消费者用完 / 生产者放弃 -> 清空槽位 */
static void slot_release(frame_slot_t *slots, int idx)
{
    xSemaphoreTake(s_slot_mutex, portMAX_DELAY);
    slots[idx].ready = false;
    slots[idx].busy  = false;
    xSemaphoreGive(s_slot_mutex);
}


/* ---------------- Wi-Fi ---------------- */

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
#if !WIFI_MODE_AP
        esp_wifi_connect();
        ESP_LOGI(TAG, "WiFi STA start, connecting to \"%s\"", WIFI_SSID);
#endif
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT);
#if !WIFI_MODE_AP
        /*
         * 只有 STA 模式才需要重连。AP 模式下没有 STA 连接,
         * 在这里反复 esp_wifi_connect() 只会刷屏, 还会反复打断摄像头 GDMA。
         *
         * 失败原因码是排障关键:
         *   201 = AP not found          (SSID 拼错 / 信号不在 / AP 隐藏)
         *   202 = Auth failure         (密码错 / 加密方式不匹配)
         *   205 = 5GHz only            (AP 只开 5GHz, ESP32-S3 只支持 2.4GHz)
         *   209 = WPA2 enterprise      (需要 PEAP/账号, ESP32 不支持)
         */
        const wifi_event_sta_disconnected_t *disc =
            (const wifi_event_sta_disconnected_t *)event_data;
        ESP_LOGW(TAG, "WiFi disconnected, reason=%d, retrying...", disc->reason);

        /*
         * 重要: 重连必须【退避】。
         * 之前失败一次就立刻重连, 每 2.4 秒一次, 每次都要重新扫描 + 重新握手。
         * 这个过程持续抢占 GDMA/总线, 直接把摄像头帧撕裂 -> 屏幕花屏。
         * 改成指数退避 (2s, 4s, 8s, 16s, 最长 30s):
         * 既不放弃连接, 又不再疯狂冲击总线。
         */
        if (s_wifi_retry_count < 6) {
            s_wifi_retry_count++;
        }
        uint32_t delay_ms = 2000u << (s_wifi_retry_count - 1);
        if (delay_ms > 30000) {
            delay_ms = 30000;
        }
        ESP_LOGW(TAG, "WiFi backoff: %" PRIu32 " ms (attempt %" PRIu32 ")", delay_ms, (uint32_t)s_wifi_retry_count);

        /* 退避等待不能在事件回调里 delay, 交给专门的任务做 */
        s_wifi_reconnect_pending = true;
        s_wifi_reconnect_delay_ms = delay_ms;
        xEventGroupSetBits(s_wifi_events, WIFI_RECONNECT_BIT);
#endif
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "已连接 Wi-Fi，内网 IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_AP_START) {
        ESP_LOGI(TAG, "热点已启动，SSID: %s", WIFI_SSID);
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init(void)
{
    s_wifi_events = xEventGroupCreate();

    /*
     * 初始化顺序不能错:
     *   1) esp_netif_init()
     *   2) esp_event_loop_create_default()  <- 少了这行，后面所有
     *      esp_event_handler_register() 都会返回 ESP_ERR_INVALID_STATE
     *   3) esp_wifi_init()
     *   4) esp_netif_create_default_wifi_sta() / _ap()
     */
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

#if WIFI_MODE_AP
    /* ---- AP 模式: 板子自己开热点, IP 固定 192.168.4.1 ----
     * 注意顺序: esp_wifi_set_config() 必须在 esp_wifi_set_mode() 之前,
     * 否则会返回 ESP_ERR_WIFI_MODE (0x3005)。参照 IDF 官方 softap_sta 例程。
     */
    s_netif = esp_netif_create_default_wifi_ap();

    wifi_config_t ap_cfg = {
        .ap = {
            .ssid     = WIFI_SSID,
            .ssid_len = strlen(WIFI_SSID),
            .channel  = 1,
            .password = WIFI_PASSWORD,
            .max_connection = 4,
            .authmode = WIFI_AUTH_WPA2_PSK,
            .pmf_cfg = {
                .required = false,
            },
        },
    };
    /*
     * 注意顺序: 必须先 esp_wifi_set_mode() 再 esp_wifi_set_config()。
     * esp_wifi_init() 之后模式是 WIFI_MODE_NULL，此时 set_config(WIFI_IF_AP)
     * 会返回 ESP_ERR_WIFI_MODE (0x3005)。IDF 官方 softap_sta 例程用
     * WIFI_MODE_APSTA，AP 单独模式在这版 IDF 上不被接受，这里跟着用 APSTA。
     */
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Wi-Fi 热点模式启动中 (SSID=%s)", WIFI_SSID);
#else
    /* ---- STA 模式: 板子连办公室 Wi-Fi ---- */
    s_netif = esp_netif_create_default_wifi_sta();

    wifi_config_t sta_cfg = {
        .sta = {
            .ssid     = WIFI_SSID,
            .password = WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Wi-Fi STA 模式启动中 (SSID=%s)", WIFI_SSID);
#endif
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

/* 板载电源使能 + 背光 + 摄像头复位 (走 AW9523B) */
static void camera_power_reset(void)
{
    if (aw9523b_init(BOARD_I2C_PORT, AW9523B_I2C_ADDR) != ESP_OK) {
        ESP_LOGW(TAG, "AW9523B 不可用，跳过电源/背光/复位控制");
        return;
    }

    /*
     * 严格照抄正点原子官方 atk_dnesp32s3_box3.cc 的 InitializeIoExpander()：
     *   - 方向: DRV_IO_EXP_OUTPUT_MASK = 0xFFFC (除 K1/K2 外全部输出)
     *            DRV_IO_EXP_INPUT_MASK  = 0x0003 (K1/K2 是输入)
     *   - 电源: XIO_VDD_2V8_EN=1 (摄像头), XIO_VDD_3V3_EN=1 (主电源),
     *            XIO_VDDA_3V3_EN=1 (音频), XIO_VBAT_EN=1, XIO_PA_CTRL=1,
     *            XIO_ESP_ADC_SEL=1
     *   - 背光: XIO_LCD_BL=0  ← 关键！背光是低电平点亮，
     *            AW9523B 上电默认输出锁存器全高，不显式拉低屏幕就是黑的
     */
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_KEY_K1, false);
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_KEY_K2, false);

    static const uint8_t power_outputs[] = {
        XIO_ESP_ADC_SEL,  /* XIO4  ADC 选择 */
        XIO_PA_CTRL,      /* XIO5  音频功放 */
        XIO_LCD_BL,       /* XIO8  背光 */
        XIO_VDD_3V3_EN,   /* XIO11 板载 3.3V 总电源 */
        XIO_VBAT_EN,      /* XIO12 音频 VBAT */
        XIO_VDDA_3V3_EN,  /* XIO13 音频 3.3V */
        XIO_CAM_2V8_EN,   /* XIO14 摄像头 2.8V */
        XIO_CAM_RESET,    /* XIO15 摄像头复位 */
    };
    for (size_t i = 0; i < sizeof(power_outputs) / sizeof(power_outputs[0]); i++) {
        aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, power_outputs[i], true);
    }

    /* 电源类全部拉高 */
    static const uint8_t power_high[] = {
        XIO_ESP_ADC_SEL,
        XIO_PA_CTRL,
        XIO_VDD_3V3_EN,
        XIO_VBAT_EN,
        XIO_VDDA_3V3_EN,
        XIO_CAM_2V8_EN,
    };
    for (size_t i = 0; i < sizeof(power_high) / sizeof(power_high[0]); i++) {
        aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, power_high[i], true);
    }

    /* 背光: 低电平点亮 (官方 InitializeIoExpander 明确写 0) */
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_LCD_BL, false);

    vTaskDelay(pdMS_TO_TICKS(100));

    /* 摄像头复位时序: 拉低保持 50ms -> 拉高等待 50ms */
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_CAM_RESET, false);
    vTaskDelay(pdMS_TO_TICKS(50));
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_CAM_RESET, true);
    vTaskDelay(pdMS_TO_TICKS(50));

    /* 调试: 回读寄存器，确认电源/复位/背光位真的写进去了 */
    uint8_t cfg0 = 0, cfg1 = 0, out0 = 0, out1 = 0;
    if (aw9523b_get_config(BOARD_I2C_PORT, AW9523B_I2C_ADDR, &cfg0, &cfg1) == ESP_OK &&
        aw9523b_get_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, &out0, &out1) == ESP_OK) {
        ESP_LOGI(TAG, "AW9523B CONFIG: P0=0x%02x P1=0x%02x | OUTPUT: P0=0x%02x P1=0x%02x",
                 cfg0, cfg1, out0, out1);
        ESP_LOGI(TAG, "  期望: P1 bit3(3V3)=1 bit4(VBAT)=1 bit5(VDDA)=1 bit6(2V8)=1 bit7(RST)=1 bit0(BL)=0");
    }
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

    .xclk_freq_hz = 24000000,   /* 正点原子官方示例用 24MHz */
    .ledc_timer   = LEDC_TIMER_0,
    .ledc_channel = LEDC_CHANNEL_0,

    .pixel_format = PIXFORMAT_RGB565,
    .frame_size   = FRAMESIZE_QVGA,   /* 320x240 */
    .fb_count     = 4,
    .fb_location  = CAMERA_FB_IN_PSRAM,
    .grab_mode    = CAMERA_GRAB_LATEST,

    /* SCCB 复用共享 I2C 总线 (SDA=3/SCL=2) */
    .sccb_i2c_port = BOARD_I2C_PORT,
};

/* 摄像头诊断: 直接在 SCCB 上读 GC0308 的 PID, 定位探测失败原因 */
static void camera_diag_sccb(void)
{
    uint8_t reg = 0xfe;
    uint8_t dummy = 0x00;

    /* GC0308 detect 流程: 先写 0xFE=0x00, 再读 0x00 得到 PID */
    esp_err_t err = i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                               &reg, 1, 100 / portTICK_PERIOD_MS);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SCCB: 写 0xFE 失败: %s", esp_err_to_name(err));
    }

    err = i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                     &dummy, 1, 100 / portTICK_PERIOD_MS);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SCCB: 写 0x00 失败: %s", esp_err_to_name(err));
    }

    uint8_t pid = 0;
    err = i2c_master_read_from_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                      &pid, 1, 100 / portTICK_PERIOD_MS);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SCCB: 读 PID 失败 (0x%02x): %s", CAM_SCCB_ADDR, esp_err_to_name(err));
        return;
    }
    ESP_LOGI(TAG, "SCCB: 0x%02x 读回 PID=0x%02x (GC0308 期望 0x9b)", CAM_SCCB_ADDR, pid);
}

static void camera_init(void)
{
    camera_diag_sccb();

    esp_err_t err = esp_camera_init(&camera_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "摄像头初始化失败: 0x%x", err);
        ESP_LOGE(TAG, "  排查: 1)PSRAM Octal 2)SCCB 复用 I2C 是否已初始化 3)GC0308 供电/复位");
        return;
    }

    sensor_t *s = esp_camera_sensor_get();
    if (s != NULL) {
        /* GC0308 的 PID 是 0x9b（不是 0x9a，官方例程同样是 0x9b） */
        ESP_LOGI(TAG, "摄像头型号 PID: 0x%04x (期望 GC0308 = 0x9b)", s->id.PID);
        /*
         * 不做 vflip / hmirror。
         *
         * 正点原子 BOX3 的官方配置是:
         *   DISPLAY_SWAP_XY = true, DISPLAY_MIRROR_X = true, DISPLAY_MIRROR_Y = false
         * 也就是说方向修正全部在 LCD 侧完成, 摄像头侧保持传感器原生朝向。
         * 我们这边 LCD 的 swap_xy / mirror 参数与官方完全一致,
         * 所以摄像头侧再翻一次就会上下颠倒 (之前加 set_vflip(s,1) 导致画面反了)。
         */
        s->set_brightness(s, 1);
    }
    ESP_LOGI(TAG, "摄像头初始化成功");
}

/* ---------------- 摄像头生产者任务 ---------------- */

/*
 * 唯一碰摄像头的任务。
 *
 * 流程:
 *   esp_camera_fb_get()  -> 拷进自己的 RGB565 槽位 (稳定副本)
 *                        -> 编码成 JPEG 放进 JPEG 槽位
 *                        -> 归还摄像头帧缓冲
 *
 * 关键点:
 *   1) 拷进自己的槽位再给下游用。摄像头是连续 DMA, fb_count=2 时下一帧的
 *      DMA 可能在我们还没用完这一帧时就覆盖了它 -> 花屏。拷成副本后
 *      LCD 和视频流拿到的都是稳定数据, 花屏从根上消失。
 *   2) JPEG 编码放在这里做, 而不是放在 HTTP 线程里:
 *      编码是 CPU 密集的活, 放在 HTTP 线程会让同一个线程既编码又等 socket,
 *      而且多客户端时每个客户端都要重复编码一遍 (CPU 直接翻倍)。
 *      现在只编码一次, 所有客户端共用同一个 JPEG 帧。
 */
static void camera_producer_task(void *arg)
{
    int frames = 0;
    int64_t t0 = 0;

    while (true) {
        int64_t t_start = esp_timer_get_time();

        camera_fb_t *fb = esp_camera_fb_get();
        if (fb == NULL) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        s_cam_get_us = (uint32_t)(esp_timer_get_time() - t_start);

        /*
         * 看门狗诊断: task_wdt 报 "CPU 1: cam_producer" 说明这个任务
         * 连续很久没让出 CPU。分别记录 "取帧耗时" 和 "编码耗时" 就能定位。
         * 取帧如果卡住, 说明是 fb_count/GRAB_LATEST 的队列逻辑在绕圈。
         */

        if (fb->format != PIXFORMAT_RGB565) {
            ESP_LOGW(TAG, "生产者: 期望 RGB565，实际 %d", (int)fb->format);
            esp_camera_fb_return(fb);
            continue;
        }

        /*
         * 帧完整性检测 (自动发现花屏, 不用靠眼睛看)
         *
         * 原理: 摄像头是连续 DMA, 我们在 memcpy 帧缓冲的同时,
         * 下一帧的 DMA 可能已经开始往同一块缓冲区写了。
         * 如果在拷贝【前】和拷贝【后】对帧缓冲算一次 CRC,
         * 两次不一致 => 这一帧在拷贝过程中被改写了 => 撕裂。
         *
         * 对比: 之前是把"帧"当成静态数据用, 撕裂只在画面上表现为花屏,
         *       根本分不清是摄像头出的问题还是显示出的问题。
         */
        uint32_t crc_before = 0, crc_after = 0;
        {
            const uint8_t *b = (const uint8_t *)fb->buf;
            size_t n = (size_t)fb->width * fb->height * 2;
            for (size_t i = 0; i < n; i++) {
                crc_before = (crc_before ^ b[i]) * 16777619u;
            }
        }

        /*
         * 逐行亮度剖面 —— 把"花屏"变成可读的数字。
         *
         * 每 16 行算一个平均亮度并打出来。
         *   正常画面: 相邻行的亮度差很小 (画面是连续的自然场景)
         *   花  屏  : 某些行亮度突变, 或者出现 0 / 255 这种极端值
         *
         * 价值: 不用盯屏幕猜"哪里花了", 直接看数字定位到具体的行。
         * 之前所有排查都卡在"只能肉眼看"这一步。
         */
        if (frames == 2) {
            const uint8_t *b = (const uint8_t *)fb->buf;
            int w = fb->width;
            for (int y0 = 0; y0 < fb->height; y0 += 16) {
                uint32_t sum = 0;
                int rows = (fb->height - y0 < 16) ? (fb->height - y0) : 16;
                for (int y = y0; y < y0 + rows; y++) {
                    for (int x = 0; x < w; x++) {
                        sum += b[(size_t)y * w * 2 + x * 2];   /* 高字节约等于亮度 */
                    }
                }
                uint32_t avg = sum / (uint32_t)(rows * w);
                ESP_LOGI(TAG, "ROW %3d: avg=%3u%s", y0, (unsigned)avg,
                         (avg == 0 || avg >= 250) ? "  <== 极端值, 可疑!" : "");
            }
        }

        /* ---- 1) 拷进 RGB565 槽位 (稳定副本) ---- */
        int rgb_idx = -1;
        if (slot_acquire(s_rgb_slots, &rgb_idx)) {
            int64_t t_copy = esp_timer_get_time();
            memcpy(s_rgb_slots[rgb_idx].buf, fb->buf, RGB565_SLOT_BYTES);
            s_rgb_slots[rgb_idx].len = RGB565_SLOT_BYTES;
            s_rgb_copy_us = (uint32_t)(esp_timer_get_time() - t_copy);

            {
                const uint8_t *b = (const uint8_t *)fb->buf;
                size_t n = (size_t)fb->width * fb->height * 2;
                for (size_t i = 0; i < n; i++) {
                    crc_after = (crc_after ^ b[i]) * 16777619u;
                }
            }
            if (crc_before != crc_after) {
                s_torn_frames++;
            } else {
                s_clean_frames++;
            }

            /*
             * ---- 2) 按需编码 JPEG ----
             *
             * 为什么"按需":
             *   软件 JPEG 编码要 65ms/帧 (占整帧 71ms 的 92%),
             *   而 ESP32-S3 没有硬件 JPEG 编码器, 这个开销躲不掉。
             *
             *   但绝大多数时候【没有任何人在看远程流】(只有本机 LCD 预览),
             *   这时候编码纯浪费 —— 还会把 CPU 占满, 导致 LCD 刷新卡顿、
             *   触发看门狗, 间接造成画面撕裂。
             *
             * 所以: 只有真的有人拉流 (s_stream_clients > 0) 才编码。
             *   本地使用: 0% 的编码开销, 预览流畅
             *   远程推流: 正常编码, 约 13fps (受限于 CPU, 无法更高)
             */
            if (s_stream_clients > 0) {
                int jpg_idx = -1;
                if (slot_acquire(s_jpg_slots, &jpg_idx)) {
                    int64_t t_enc = esp_timer_get_time();
                    size_t jlen = encode_jpeg_to_slot(s_rgb_slots[rgb_idx].buf,
                                                      fb->width, fb->height,
                                                      s_jpg_slots[jpg_idx].buf);
                    s_jpg_encode_us = (uint32_t)(esp_timer_get_time() - t_enc);

                    if (jlen > 0) {
                        s_jpg_slots[jpg_idx].len = jlen;
                        s_jpg_bytes = (uint32_t)jlen;
                        s_jpg_frames++;
                        slot_publish(s_jpg_slots, jpg_idx);
                    } else {
                        slot_release(s_jpg_slots, jpg_idx);
                    }
                }
            } else {
                s_jpg_encode_us = 0;   /* 未编码 */
            }

            slot_publish(s_rgb_slots, rgb_idx);
        }

        esp_camera_fb_return(fb);

        frames++;
        s_cam_frames++;
        if (frames == 1) {
            t0 = esp_timer_get_time();
        } else if (frames % 50 == 0) {
            int64_t dt_us = esp_timer_get_time() - t0;
            ESP_LOGI(TAG, "PROD: %d frames (%.1f fps) | get %" PRIu32 "us copy %" PRIu32 "us jpeg %" PRIu32 "us | jpg %" PRIu32 " bytes | TORN %" PRIu32 "/%" PRIu32,
                     frames, (frames - 1) * 1000000.0 / (double)dt_us,
                     s_cam_get_us, s_rgb_copy_us,
                     s_jpg_encode_us, s_jpg_bytes,
                     s_torn_frames,
                     (uint32_t)(s_torn_frames + s_clean_frames));
        }
    }
}

/* ---------------- LCD 预览任务 (消费者) ---------------- */

/*
 * 只从 RGB565 槽位取现成的帧, 不碰摄像头。
 * 降频到 ~2 fps —— LCD 预览是给老人自己看的"我在线"指示,
 * 不需要高帧率; 把 CPU 全留给远程视频流。
 */
/*
 * LCD 显示模式 (运行时可切换, 用串口命令改, 不用反复编译烧录)
 *
 *   MODE_CAMERA  正常: 显示摄像头画面
 *   MODE_PATTERN 显示固定测试图样 (完全不碰摄像头)
 *   MODE_SYNTH   显示程序生成的合成渐变图 (走和摄像头一样的 draw_rgb565 路径)
 *
 * 为什么要有这个:
 *   "花屏" 有两种可能, 肉眼分不开:
 *     A) LCD/SPI 链路本身有问题 (批次边界错位, DMA 复用)
 *     B) 摄像头数据本身是乱的
 *   MODE_PATTERN 一开就能一刀切开: 图样也花 = A, 图样正常 = B。
 *   MODE_SYNTH 再进一步区分"画图代码路径"和"摄像头数据"。
 */
typedef enum {
    LCD_MODE_CAMERA = 0,
    LCD_MODE_PATTERN = 1,
    LCD_MODE_SYNTH = 2,
} lcd_mode_t;

static volatile lcd_mode_t s_lcd_mode = LCD_MODE_CAMERA;

static void lcd_preview_task(void *arg)
{
    while (true) {
        if (s_lcd_mode == LCD_MODE_PATTERN) {
            /* 固定图样: 不依赖摄像头, 每秒重画一次 */
            lcd_draw_test_pattern();
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        if (s_lcd_mode == LCD_MODE_SYNTH) {
            /* 合成图: 走和摄像头完全一样的 lcd_draw_rgb565 路径, 但数据是程序生成的 */
            lcd_draw_synthetic_image(true /* 源是大端, 和摄像头一致 */);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        int idx = -1;
        size_t len = 0;

        if (slot_take_ready(s_rgb_slots, &idx, &len)) {
            /* 正常整屏显示 (真实产品路径) */
            lcd_draw_rgb565(s_rgb_slots[idx].buf, 320 * 2, 0, 0, 320, 240, true);
            slot_release(s_rgb_slots, idx);
        } else {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        vTaskDelay(pdMS_TO_TICKS(LCD_PREVIEW_MS));
    }
}

/* ---------------- JPEG 编码 (流式, 零额外拷贝) ---------------- */

/*
 * 用 frame2jpg_cb / fmt2jpg_cb 而不是 frame2jpg:
 *   frame2jpg     每帧 malloc 128KB -> 编码 -> memcpy 到缓冲 -> free
 *                 在 PSRAM 上反复大块分配/释放, 既慢又引起碎片
 *   fmt2jpg_cb    编码器直接把 512 字节一块的数据推给回调, 我们顺手写进
 *                 预分配的槽位, 没有额外拷贝, 也没有每帧 malloc
 *
 * fmt2jpg_cb 支持 PIXFORMAT_RGB565 输入, to_jpg.cpp 内部会逐行展开成
 * RGB888 (用的是一个行缓冲区, 不是整帧中间缓冲), 然后 jpge 再转 YCC。
 * 所以直接喂 RGB565, 不要自己先展开成整帧 RGB888 —— 那会多占 230KB。
 */
typedef struct {
    uint8_t *dst;
    size_t   cap;
    size_t   written;
} jpg_sink_t;

/*
 * 回调返回实际写入的字节数; 返回 0 表示写满了, jpge 会停止继续输出。
 * 注意 jpge 用返回值累加 index, 所以这里必须返回实际写入量。
 */
static size_t jpg_sink_cb(void *arg, size_t index, const void *data, size_t len)
{
    jpg_sink_t *sink = (jpg_sink_t *)arg;

    if (index + len > sink->cap) {
        len = sink->cap - index;   /* 写满就截断 */
        if (len == 0) {
            return 0;
        }
    }
    memcpy(sink->dst + index, data, len);
    sink->written = index + len;
    return len;
}

/*
 * 把一帧 RGB565 编码成 JPEG, 写进 dst。
 * 返回写入的字节数 (0 = 失败)。
 *
 * 注意: 摄像头帧缓冲是大端 (高位字节在前), jpge 默认 rgb565_big_endian = true,
 * 与我们的帧缓冲格式一致, 不用做字节交换。
 */
static size_t encode_jpeg_to_slot(const uint8_t *rgb565, int w, int h, uint8_t *dst)
{
    jpg_sink_t sink = {
        .dst = dst,
        .cap = JPEG_SLOT_BYTES,
        .written = 0,
    };

    if (!fmt2jpg_cb((uint8_t *)rgb565, (size_t)w * h * 2, (uint16_t)w, (uint16_t)h,
                    PIXFORMAT_RGB565, JPEG_QUALITY, jpg_sink_cb, &sink)) {
        return 0;
    }
    return sink.written;
}


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

    /*
     * 这里【不再】自己做 JPEG 编码 —— 生产者任务已经编好了, 我们只负责取。
     *
     * 好处:
     *   1) 多客户端时 CPU 只编码一次 (之前每个客户端各编一遍, 客户端越多越卡)
     *   2) HTTP 线程不再被几十毫秒的编码阻塞, 发送更及时
     *   3) 没有每帧 malloc(128KB) + memcpy
     */
    while (true) {
        int idx = -1;
        size_t jpg_len = 0;

        if (!slot_take_ready(s_jpg_slots, &idx, &jpg_len)) {
            /* 还没生产出新帧, 等等 */
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

        char part_buf[96];
        int len = snprintf(part_buf, sizeof(part_buf),
                           "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                           (unsigned int)jpg_len);
        bool ok = (httpd_resp_send_chunk(req, part_buf, len) == ESP_OK);

        if (ok) {
            ok = (httpd_resp_send_chunk(req, (const char *)s_jpg_slots[idx].buf, jpg_len) == ESP_OK);
        }
        if (ok) {
            ok = (httpd_resp_send_chunk(req, "\r\n", 2) == ESP_OK);
        }

        slot_release(s_jpg_slots, idx);

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

    char resp[512];
    snprintf(resp, sizeof(resp),
             "{\"ip\":\"" IPSTR "\",\"uptime\":%llu,\"frames\":%" PRIu32 ",\"clients\":%d,"
             "\"psram\":%" PRIu32 ","
             "\"cam_frames\":%" PRIu32 ",\"jpg_frames\":%" PRIu32 ",\"jpg_bytes\":%" PRIu32 ","
             "\"cam_get_us\":%" PRIu32 ",\"rgb_copy_us\":%" PRIu32 ",\"jpg_encode_us\":%" PRIu32 ","
             "\"torn_frames\":%" PRIu32 ","
             "\"jpeg_quality\":%d,\"max_fps\":%" PRIu32 "}",
             IP2STR(&ip.ip),
             (unsigned long long)(esp_timer_get_time() / 1000),
             (uint32_t)s_frame_count,
             s_stream_clients,
             (uint32_t)esp_psram_get_size(),
             (uint32_t)s_cam_frames,
             (uint32_t)s_jpg_frames,
             (uint32_t)s_jpg_bytes,
             (uint32_t)s_cam_get_us,
             (uint32_t)s_rgb_copy_us,
             (uint32_t)s_jpg_encode_us,
             (uint32_t)s_torn_frames,
             JPEG_QUALITY,
             (uint32_t)(s_jpg_encode_us > 0 ? 1000000 / s_jpg_encode_us : 0));

    httpd_resp_set_type(req, "application/json; charset=utf-8");
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

/* ---------------- 任务创建辅助 ---------------- */

/*
 * 内部 RAM 很紧张 (Wi-Fi 驱动 + 相机 + LCD 都要内部内存)，
 * FreeRTOS 默认从内部 RAM 分任务栈，很容易 xTaskCreate 失败
 * (errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY = -1)。
 * 所以这里显式从 PSRAM 分配栈，再手动建任务。
 */
static void task_start(void *fn, const char *name, uint32_t stack_bytes, UBaseType_t prio)
{
    /*
     * ESP-IDF 5.x 的 xTaskCreateStatic 签名:
     *   xTaskCreateStatic(fn, name, ulStackDepth(字节), pvParameters, prio, puxStackBuffer, pxTaskBuffer)
     * 注意和原生 FreeRTOS 不同: 栈深度单位是字节, 且需要额外的 StaticTask_t。
     */
    StackType_t *stack = heap_caps_malloc(stack_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (stack == NULL) {
        ESP_LOGE(TAG, "任务 %s 的栈分配失败 (PSRAM)", name);
        return;
    }

    /*
     * TCB (任务控制块) 必须放内部 RAM —— FreeRTOS 内核会直接访问它，
     * 放 PSRAM 会触发 xPortCheckValidTCBMem 断言。
     * 栈才是大头，放 PSRAM 没问题 (需 CONFIG_FREERTOS_TASK_STACK_ALLOW_EXT_MEM)。
     */
    StaticTask_t *tcb = heap_caps_malloc(sizeof(StaticTask_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (tcb == NULL) {
        ESP_LOGE(TAG, "任务 %s 的 TCB 分配失败 (内部RAM)", name);
        heap_caps_free(stack);
        return;
    }

    TaskHandle_t handle = xTaskCreateStatic((TaskFunction_t)fn, name, stack_bytes,
                                            NULL, prio, stack, tcb);
    if (handle == NULL) {
        ESP_LOGE(TAG, "任务 %s 创建失败", name);
        heap_caps_free(stack);
        heap_caps_free(tcb);
        return;
    }
    ESP_LOGI(TAG, "任务 %s 已启动", name);
}

/* ---------------- Wi-Fi 重连任务 (带退避) ---------------- */

/*
 * 单独一个任务做重连, 而不是直接在事件回调里 esp_wifi_connect()。
 *
 * 原因: 事件回调跑在 Wi-Fi 驱动的事件任务里, 在那里做延时/重连会
 * 阻塞其他 Wi-Fi 事件, 而且重连本身要重新扫描 + 重新握手, 期间会
 * 抢占 GDMA 总线 -> 撕裂摄像头帧 -> 屏幕花屏。
 *
 * 退避策略: 2s -> 4s -> 8s -> 16s -> 32s(封顶 30s)
 */
static void wifi_reconnect_task(void *arg)
{
    while (true) {
        EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_RECONNECT_BIT,
                                               pdFALSE, pdTRUE, portMAX_DELAY);
        if (!(bits & WIFI_RECONNECT_BIT)) {
            continue;
        }
        xEventGroupClearBits(s_wifi_events, WIFI_RECONNECT_BIT);

        uint32_t delay_ms = s_wifi_reconnect_delay_ms;
        ESP_LOGI(TAG, "WiFi reconnecting in %" PRIu32 " ms...", delay_ms);
        vTaskDelay(pdMS_TO_TICKS(delay_ms));

        if (s_wifi_reconnect_pending) {
            s_wifi_reconnect_pending = false;
            esp_err_t err = esp_wifi_connect();
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "esp_wifi_connect failed: %s", esp_err_to_name(err));
            }
        }
    }
}

/* ---------------- AW9523B 诊断任务 ---------------- */

/*
 * 蓝灯慢速心跳，确认 AW9523B 输出通路正常。
 * 背光保持常亮（由 camera_power_reset 设置为低电平点亮）。
 */
static void aw9523b_diag_task(void *arg)
{
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_LED_BLUE, true);

    while (true) {
        aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_LED_BLUE, true);
        vTaskDelay(pdMS_TO_TICKS(600));
        aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_LED_BLUE, false);
        vTaskDelay(pdMS_TO_TICKS(1400));
    }
}

/* ---------------- 串口调试命令 ---------------- */

/*
 * 排障用的调试命令 (通过 USB 串口输入, 换行执行):
 *
 *   lcd0  显示摄像头画面 (默认)
 *   lcd1  显示固定测试图样 (不碰摄像头)  -> 判定 LCD/SPI 链路好坏
 *   lcd2  显示合成渐变图 (走 draw_rgb565 路径) -> 判定画图代码路径好坏
 *   stat  打印性能与撕裂统计
 *
 * 为什么用运行时命令而不是编译期宏:
 *   花屏排查需要反复切换对照, 每次改宏都要重新编译烧录 (约 2 分钟),
 *   改命令则只要敲一行, 当场就能看到结果。
 */
static void uart_cmd_task(void *arg)
{
    char line[64];
    int  len = 0;

    while (true) {
        uint8_t ch;
        int n = read(STDIN_FILENO, &ch, 1);
        if (n != 1) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (ch == '\r' || ch == '\n') {
            if (len > 0) {
                line[len] = '\0';
                if (strcmp(line, "lcd0") == 0) {
                    s_lcd_mode = LCD_MODE_CAMERA;
                    ESP_LOGI(TAG, "LCD 模式: 摄像头画面");
                } else if (strcmp(line, "lcd1") == 0) {
                    s_lcd_mode = LCD_MODE_PATTERN;
                    ESP_LOGI(TAG, "LCD 模式: 固定测试图样");
                } else if (strcmp(line, "lcd2") == 0) {
                    s_lcd_mode = LCD_MODE_SYNTH;
                    ESP_LOGI(TAG, "LCD 模式: 合成渐变图");
                } else if (strcmp(line, "stat") == 0) {
                    ESP_LOGI(TAG, "cam_frames=%u jpg_frames=%u jpg_bytes=%u torn=%u clean=%u",
                             (unsigned)s_cam_frames, (unsigned)s_jpg_frames,
                             (unsigned)s_jpg_bytes,
                             (unsigned)s_torn_frames, (unsigned)s_clean_frames);
                } else {
                    ESP_LOGW(TAG, "未知命令: %s (可用: lcd0/lcd1/lcd2/stat)", line);
                }
                len = 0;
            }
            continue;
        }
        if (ch >= 32 && ch < 127 && len < (int)sizeof(line) - 1) {
            line[len++] = (char)ch;
        }
    }
}

/* ---------------- 按键任务 ---------------- */

/*
 * 触发源:
 *   1) BOOT 键 (GPIO0, 板载直连)
 *   2) K1 / K2 键 (AW9523B 扩展器上的 XIO0 / XIO1, 低有效)
 * 任一按下都会产生一个事件, 网页弹出提示语。
 *
 * 注意: K1/K2 的方向 (输入) 已在 camera_power_reset() 里设好, 这里不再重复写。
 * 每 50ms 轮询一次, 如果每次都做「读 CONFIG + 写 CONFIG」的读改写,
 * 会和摄像头的 SCCB 抢同一条 I2C 总线 (SDA=3/SCL=2), 反而更容易掉帧。
 */
static bool aw_key_pressed(void)
{
    bool high = true;

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

    ESP_LOGI(TAG, "PSRAM: %u bytes, 剩余堆: %u bytes",
             (unsigned int)esp_psram_get_size(),
             (unsigned int)esp_get_free_heap_size());

    board_i2c_init();
    camera_power_reset();

    /* 屏幕: 背光已在 AW9523B 里打开, 这里初始化面板驱动 */
    bool lcd_ok = (lcd_init() == ESP_OK);
    if (lcd_ok) {
        lcd_fill_screen(0x0000);      /* 先清黑，等摄像头画面 */
        ESP_LOGI(TAG, "屏幕已初始化，等待摄像头画面");
    } else {
        ESP_LOGE(TAG, "屏幕初始化失败（不影响摄像头功能）");
    }

#if LCD_SELFTEST_ONLY == 0
    /*
     * 正常模式: 初始化帧槽位 + 摄像头。
     *
     * 注意: 之前 camera_init() 只在诊断模式里被调用过, 正常模式漏掉了,
     * 也就是说 httpd 起来之后摄像头根本没初始化 —— 这是个隐藏 bug。
     */
    s_slot_mutex = xSemaphoreCreateMutex();
    if (s_slot_mutex == NULL) {
        ESP_LOGE(TAG, "槽位互斥量创建失败");
        return;
    }
    for (int i = 0; i < SLOT_COUNT; i++) {
        s_rgb_slots[i].buf = heap_caps_malloc(RGB565_SLOT_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        s_jpg_slots[i].buf = heap_caps_malloc(JPEG_SLOT_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_rgb_slots[i].buf == NULL || s_jpg_slots[i].buf == NULL) {
            ESP_LOGE(TAG, "槽位 %d 内存分配失败", i);
            return;
        }
        s_rgb_slots[i].len = 0;
        s_jpg_slots[i].len = 0;
    }
    s_diag_copy_buf = heap_caps_malloc(DIAG_W * DIAG_H * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (s_diag_copy_buf == NULL) {
        ESP_LOGE(TAG, "诊断缓冲分配失败 (内部RAM)");
    } else {
        ESP_LOGI(TAG, "诊断缓冲就绪: %dx%d 内部RAM", DIAG_W, DIAG_H);
    }

    ESP_LOGI(TAG, "帧槽位就绪: %d 个 RGB565(%u字节) + %d 个 JPEG(%u字节)",
             SLOT_COUNT, (unsigned)RGB565_SLOT_BYTES,
             SLOT_COUNT, (unsigned)JPEG_SLOT_BYTES);

    camera_init();
#endif

#if LCD_SELFTEST_ONLY
    /*
     * 自检模式 (不启动 WiFi / HTTP, 把变量降到最少)。
     *
     * 模式 1: 纯色/色条图样 -> 验证 LCD/SPI 链路
     * 模式 2: 合成 PSRAM 图 -> 验证 draw_rgb565 代码路径 + PSRAM 读
     * 模式 3: 真摄像头帧   -> 验证帧数据本身, 并把结论打到串口
     */
    if (lcd_ok) {
#if LCD_SELFTEST_ONLY == 3
        /*
         * 摄像头诊断模式。
         *
         * 关键: 全程不开 WiFi。WiFi 一开就会占用内部 RAM / 抢总线,
         * 之前摄像头跑 6.5fps 也和这个有关, 排查时先把它排除。
         */
        ESP_LOGI(TAG, "==== 摄像头诊断模式 (不开 WiFi/HTTP) ====");
        camera_init();

        int frames = 0;

        while (true) {
            camera_fb_t *fb = esp_camera_fb_get();
            if (fb == NULL) {
                ESP_LOGW(TAG, "诊断: 取帧失败");
                vTaskDelay(pdMS_TO_TICKS(50));
                continue;
            }

            const uint16_t *px = (const uint16_t *)fb->buf;
            size_t npix = (size_t)fb->width * fb->height;

            /*
             * 关键检验: 绘制【前】算一次校验和, 绘制【后】再算一次。
             *
             * 摄像头帧缓冲在 PSRAM 里, 而摄像头是【连续 DMA】的:
             * 它下一帧的 DMA 可能在我们正把这一帧往 LCD 送的时候,
             * 就已经开始往同一块缓冲区里写了 (fb_count=2, 有机会撞上)。
             * 一旦撞上, LCD 收到的就是"上半帧 + 下半帧"拼接的错乱数据
             * —— 这正好表现为"有图像但一直闪花屏"。
             *
             * 浏览器那边看到的 MJPEG 是干净的, 因为它走的是
             * esp_camera_fb_get() 之后的另一条路径, 不做长时间的 LCD 搬运。
             */
            uint32_t crc_before = 0, crc_after = 0;
            {
                /* FNV-1a, 逐字节算, 快 */
                const uint8_t *b = (const uint8_t *)fb->buf;
                size_t n = (size_t)fb->width * fb->height * 2;
                for (size_t i = 0; i < n; i++) {
                    crc_before = (crc_before ^ b[i]) * 16777619u;
                }
            }

            ESP_LOGI(TAG, "帧%d: %dx%d len=%u crc_before=%08x",
                     ++frames, fb->width, fb->height, (unsigned)fb->len,
                     (unsigned)crc_before);

            /*
             * 逐行指纹: 每行算 "行内平均相邻像素差" (用正确的大端字节序读 RGB565)。
             *   - 正常图像行 -> 差值小且平滑 (几十~几百)
             *   - 撕裂/噪声行 -> 差值突然变得很大
             * 一行内所有数值打印出来, 哪个位置跳变一目了然。
             */
            {
                const uint8_t *b = (const uint8_t *)fb->buf;
                char rowbuf[24];
                int p = 0;
                for (int y = 0; y < fb->height && p < (int)sizeof(rowbuf) - 6; y++) {
                    const uint8_t *row = b + (size_t)y * fb->width * 2;
                    uint64_t dsum = 0;
                    uint16_t pv = 0;
                    for (int x = 0; x < fb->width; x++) {
                        uint16_t v = (uint16_t)((row[x * 2] << 8) | row[x * 2 + 1]);
                        if (x) {
                            int d = (int)v - (int)pv;
                            if (d < 0) { d = -d; }
                            dsum += (uint64_t)d;
                        }
                        pv = v;
                    }
                    uint32_t avg = (uint32_t)(dsum / fb->width);
                    p += snprintf(rowbuf + p, sizeof(rowbuf) - p, "%s%u",
                                  (y % 8 == 0) ? "|" : ",",
                                  (unsigned)avg);
                }
                rowbuf[p] = '\0';
                ESP_LOGI(TAG, "      行内差(每8行分隔): %s", rowbuf);
            }

            /* 绘制到屏幕 —— 这一步会花掉几十毫秒 */
            lcd_draw_rgb565(fb->buf, fb->width * 2, 0, 0, fb->width, fb->height, true);

            /* 绘制【后】再算一次 */
            {
                const uint8_t *b = (const uint8_t *)fb->buf;
                size_t n = (size_t)fb->width * fb->height * 2;
                for (size_t i = 0; i < n; i++) {
                    crc_after = (crc_after ^ b[i]) * 16777619u;
                }
            }

            if (crc_before != crc_after) {
                ESP_LOGE(TAG, "  *** 帧缓冲在绘制期间被改写! before=%08x after=%08x  <-- 花屏根因",
                         (unsigned)crc_before, (unsigned)crc_after);
            } else {
                ESP_LOGI(TAG, "  帧缓冲绘制期间未被改写 (crc 一致)");
            }

            esp_camera_fb_return(fb);
            vTaskDelay(pdMS_TO_TICKS(500));
        }
#else
        ESP_LOGI(TAG, "==== LCD 自检模式 (不启动摄像头/WiFi) ====");
        int i = 0;
        while (true) {
#if LCD_SELFTEST_ONLY == 2
            /* 合成图模式: 走和摄像头一样的 lcd_draw_rgb565 路径, 但数据是程序生成的 */
            lcd_draw_synthetic_image(true /* 和摄像头当前一致: 源是大端 */);
            ESP_LOGI(TAG, "合成图 (swap_bytes=true) 第 %d 帧已刷新", ++i);
#else
            lcd_draw_test_pattern();
            ESP_LOGI(TAG, "自检图样第 %d 帧已刷新", ++i);
#endif
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
#endif
    } else {
        ESP_LOGE(TAG, "LCD 初始化失败，无法自检");
    }
#endif

    /*
     * Wi-Fi 总开关。
     *
     * 排障用: 之前的花屏一直查不清, 最大的怀疑对象是 Wi-Fi。
     * esp32-camera 的 README 明确说过: RGB/YUV 格式 + 开 Wi-Fi 时,
     * PSRAM DMA 会被 Wi-Fi 打断, 出现整帧缺行 (FB-SIZE 不匹配)。
     * 把这个开关关掉再测一次, 就能一刀切开"是 Wi-Fi 的锅还是别的锅"。
     *
     * 0 = 完全不开 WiFi (纯本地显示, 最干净的对照组)
     * 1 = 正常开 WiFi
     */
#ifndef WIFI_ENABLE
#define WIFI_ENABLE 0
#endif

#if WIFI_ENABLE
    wifi_init();
#else
    ESP_LOGW(TAG, "WiFi DISABLED by WIFI_ENABLE=0 (diagnostic mode)");
#endif

#if WIFI_ENABLE
#if WIFI_MODE_AP
    /* AP 模式: 热点起来就能用, IP 固定 192.168.4.1 */
    vTaskDelay(pdMS_TO_TICKS(500));
    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, "  1) 电脑连上 Wi-Fi 热点: %s", WIFI_SSID);
    ESP_LOGI(TAG, "  2) 浏览器打开: http://192.168.4.1:%d/", HTTP_PORT);
    ESP_LOGI(TAG, "  3) 画面地址:   http://192.168.4.1:%d/stream", HTTP_PORT);
    ESP_LOGI(TAG, "========================================");
#else
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
#endif
#endif /* WIFI_ENABLE */

#if WIFI_ENABLE
    http_server_start();
#endif

    task_start(button_task, "button_task", 4096, 5);
    task_start(aw9523b_diag_task, "aw_diag", 4096, 4);
#if WIFI_ENABLE && !WIFI_MODE_AP
    task_start(wifi_reconnect_task, "wifi_recon", 4096, 4);
#endif

#if LCD_SELFTEST_ONLY == 0
    /*
     * 生产者 (唯一碰摄像头的任务) 优先级最高 —— 它一停, 整条链路都停。
     * LCD 预览优先级最低, 它只是"老人看得见"的辅助, 让位给视频流。
     */
    task_start(camera_producer_task, "cam_producer", 6144, 6);
    if (lcd_ok) {
        task_start(lcd_preview_task, "lcd_preview", 6144, 2);
    task_start(uart_cmd_task, "uart_cmd", 4096, 1);
    }
#endif

    ESP_LOGI(TAG, "内部剩余=%u 最小剩余=%u",
             (unsigned int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned int)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL));

    printf("\n固件启动完成\n\n");
}
