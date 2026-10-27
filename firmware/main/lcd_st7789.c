#include "lcd_st7789.h"

#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_heap_caps.h"

#include "camera_pins.h"

static const char *TAG = "lcd";

static esp_lcd_panel_handle_t s_panel = NULL;

/* ST7789 一帧 RGB565 是 320*240*2 = 153600 字节
 * 注意: SPI 的 max_transfer_sz 决定 DMA 缓冲大小，内部 RAM 紧张，
 * 所以按 40 行为一批传输 (320*40*2 = 25600 字节)，不用整屏缓冲。
 */
#define LCD_H_RES        LCD_WIDTH
#define LCD_V_RES        LCD_HEIGHT
#define LCD_COLOR_BYTES  16
#define LCD_PIXEL_BYTES  (LCD_COLOR_BYTES / 8)
/*
 * 每批传输多少行。
 *
 * 原来是 40 行, 整宽 320 时的批缓冲 = 320*40*2 = 25600 字节,
 * 而 max_transfer_sz 正好也定义成 25600 —— 单次 DMA 传输长度
 * 刚好等于驱动上限, 这是边界情况, 内部拆描述符时长度容易算错,
 * 表现为画面下半部分偶尔花掉 (闪一下)。
 *
 * 改成 20 行: 批缓冲 = 12800 字节, 稳稳落在上限一半,
 * 远离边界。代价只是批次多一倍 (240 行 -> 12 批), 开销可以忽略。
 */
#define LCD_BATCH_LINES  20
/*
 * SPI 单次 DMA 传输的上限。
 *
 * 注意这个值必须【严格大于】实际使用的批缓冲大小, 不要让它们相等。
 * 相等时 (传输长度 == max_transfer_sz) 属于边界情况,
 * esp-idf 的 SPI DMA 驱动在拆分描述符时长度计算可能出错,
 * 表现为画面下半部分偶尔花掉。
 * 所以这里留出余量: 上限 32KB, 实际批缓冲 320*20*2 = 12800 字节。
 */
#define LCD_FB_BYTES     (32 * 1024)

/* ---------------- DMA 传输同步 ---------------- */

/*
 * SPI 的颜色数据是【异步】传输的:
 *   esp_lcd_panel_io_tx_color() 只是把数据丢进事务队列就立刻返回,
 *   真正的 DMA 还在后台读那块 buffer。
 *   IDF 文档 (esp_lcd_panel_io.h) 明确要求:
 *     "The caller should take care of the lifecycle of the color buffer.
 *      Recycling of color buffer should be done in the callback
 *      on_color_trans_done()."
 *
 * 我们整屏只有一块 buf, 按 40 行一批轮流往里写。
 * 如果不等上一批传完就覆写, 面板收到的是新旧混合的数据,
 * 画面就被切成一条一条的横条 (每条正好 40 行 = 一个批次)。
 *
 * 注意 esp_lcd_panel_draw_bitmap() 内部先发 CASET/RASET (走 tx_param),
 * 而 tx_param 会等待队列里的事务传完 —— 但那是发生在我们【已经覆写 buf 之后】,
 * 所以救不了场, 必须自己等。
 *
 * 信号量 s_lcd_dma_done 统计"已传完的批次数"(初值 0):
 *   每发一批 -> 回调 give 一次; 每次复用 buf 前 -> take 一次。
 *   take 次数与 give 次数严格相等, 不会死锁也不会多等。
 */
static SemaphoreHandle_t s_lcd_dma_done = NULL;  /* 已传完的批次数 */
static SemaphoreHandle_t s_lcd_mutex    = NULL;  /* 互斥量: 防止多任务同时画 */

/* SPI 传完一批颜色数据时回调 (在中断上下文触发) */
static bool lcd_color_trans_done_cb(esp_lcd_panel_io_handle_t panel_io,
                                    esp_lcd_panel_io_event_data_t *edata,
                                    void *user_ctx)
{
    BaseType_t need_yield = pdFALSE;
    xSemaphoreGiveFromISR(s_lcd_dma_done, &need_yield);
    return need_yield;
}

esp_err_t lcd_init(void)
{
    /* 同步原语: 统计"已传完的批次数" */
    s_lcd_dma_done = xSemaphoreCreateCounting(10, 0);
    s_lcd_mutex    = xSemaphoreCreateMutex();
    if (s_lcd_dma_done == NULL || s_lcd_mutex == NULL) {
        ESP_LOGE(TAG, "创建 LCD 同步信号量失败");
        return ESP_ERR_NO_MEM;
    }

    spi_bus_config_t bus_cfg = {
        .mosi_io_num     = LCD_MOSI_PIN,
        .miso_io_num     = LCD_MISO_PIN,
        .sclk_io_num     = LCD_SCLK_PIN,
        .quadwp_io_num   = -1,
        .quadhd_io_num   = -1,
        .max_transfer_sz = LCD_FB_BYTES,
    };

    esp_err_t err = spi_bus_initialize(SPI2_HOST, &bus_cfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "spi_bus_initialize 失败: %s", esp_err_to_name(err));
        return err;
    }

    esp_lcd_panel_io_handle_t io = NULL;
    esp_lcd_panel_io_spi_config_t io_cfg = {
        .cs_gpio_num             = LCD_CS_PIN,
        .dc_gpio_num             = LCD_DC_PIN,
        .spi_mode                = 0,
        .pclk_hz                 = 60 * 1000 * 1000,
        .trans_queue_depth       = 7,
        /* 必须注册这个回调, 否则不知道一批颜色数据何时真正传完, buf 复用会撕裂画面 */
        .on_color_trans_done     = lcd_color_trans_done_cb,
        .user_ctx                = NULL,
        .lcd_cmd_bits            = 8,
        .lcd_param_bits          = 8,
        .flags = {
            .dc_high_on_cmd  = 0,
            .dc_low_on_data  = 0,
            .dc_low_on_param = 0,
            .octal_mode      = 0,
            .quad_mode       = 0,
            .sio_mode        = 0,
            .lsb_first       = 0,
            .cs_high_active  = 0,
        },
    };
    err = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)SPI2_HOST, &io_cfg, &io);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_lcd_new_panel_io_spi 失败: %s", esp_err_to_name(err));
        return err;
    }

    esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = -1,
        .rgb_ele_order  = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
        /*
         * 字节序用 LITTLE。
         *
         * 我们的帧缓冲是 uint16_t 数组，内存里是 [低字节, 高字节]，
         * SPI 按内存顺序发出 -> 线上是 0x1F 0x00 (蓝色 0x001F)。
         * 若面板按大端解读，会当成 0x1F00 -> 显示成绿色。
         * 所以要让面板按小端解读，才能还原出正确的 RGB565 颜色。
         * (官方 xiaozhi 用 BIG 是因为它上层 lvgl 自己做了字节交换)
         */
        .data_endian    = LCD_RGB_DATA_ENDIAN_LITTLE,
    };

    err = esp_lcd_new_panel_st7789(io, &panel_cfg, &s_panel);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_lcd_new_panel_st7789 失败: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_lcd_panel_reset(s_panel);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_lcd_panel_reset 失败: %s", esp_err_to_name(err));
    }

    err = esp_lcd_panel_init(s_panel);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_lcd_panel_init 失败: %s", esp_err_to_name(err));
        return err;
    }

    /* 官方参数: 色彩反转 + 交换 XY + 镜像 X */
    esp_lcd_panel_invert_color(s_panel, true);
    esp_lcd_panel_swap_xy(s_panel, true);
    esp_lcd_panel_mirror(s_panel, true, false);

    /*
     * 关键: esp_lcd_panel_init() 只发 SLPOUT/MADCTL/COLMOD/RAMCTRL，
     * 不会发 DISPON (0x29)。ST7789 上电后处于 sleep 模式，
     * 不显式开显示的话，背光亮但屏幕一片死黑。
     * 官方的 SpiLcdDisplay 封装类内部做了这一步。
     */
    err = esp_lcd_panel_disp_on_off(s_panel, true);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_lcd_panel_disp_on_off 失败: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "ST7789 初始化完成 (%dx%d, RGB565, DISPON 已发送)", LCD_H_RES, LCD_V_RES);
    return ESP_OK;
}

esp_err_t lcd_fill_rect(int x, int y, int w, int h, uint16_t color)
{
    if (s_panel == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > LCD_H_RES) { w = LCD_H_RES - x; }
    if (y + h > LCD_V_RES) { h = LCD_V_RES - y; }
    if (w <= 0 || h <= 0) {
        return ESP_OK;
    }

    /* 一次只处理一批行，缓冲放在内部 RAM (DMA 需要) */
    size_t buf_len = (size_t)w * LCD_BATCH_LINES * sizeof(uint16_t);
    uint16_t *buf = heap_caps_malloc(buf_len, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    if (buf == NULL) {
        return ESP_ERR_NO_MEM;
    }

    esp_err_t err = ESP_OK;
    int pending = 0;   /* 已发出但还没收到回调的批次数 */

    xSemaphoreTake(s_lcd_mutex, portMAX_DELAY);
    for (int row = 0; row < h && err == ESP_OK; row += LCD_BATCH_LINES) {
        int lines = (h - row < LCD_BATCH_LINES) ? (h - row) : LCD_BATCH_LINES;
        size_t n = (size_t)w * lines;

        /*
         * 复用 buf 前必须等上一批传完 (第一批除外, 此时没有数据在传)。
         * take 一次 = 消耗一次 give, 收支平衡, 不会死锁。
         * 只有发送成功才计数, 发送失败不会触发回调, 也不能去等。
         */
        if (pending > 0) {
            xSemaphoreTake(s_lcd_dma_done, portMAX_DELAY);
            pending--;
        }

        for (size_t i = 0; i < n; i++) {
            buf[i] = color;
        }
        /*
         * 注意 esp_lcd_panel_draw_bitmap 的 x_end / y_end 是【开区间】右边界，
         * 不是宽高！必须传 x+w 和 y+row+lines。
         */
        err = esp_lcd_panel_draw_bitmap(s_panel, x, y + row, x + w, y + row + lines, buf);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "draw_bitmap 失败 @(%d,%d): %s", x, y + row, esp_err_to_name(err));
        } else {
            pending++;
        }
    }

    /* 最后一批也要等传完才能 free(buf) */
    while (pending > 0) {
        xSemaphoreTake(s_lcd_dma_done, portMAX_DELAY);
        pending--;
    }
    xSemaphoreGive(s_lcd_mutex);
    free(buf);
    return err;
}

esp_err_t lcd_fill_screen(uint16_t color)
{
    return lcd_fill_rect(0, 0, LCD_H_RES, LCD_V_RES, color);
}

esp_err_t lcd_draw_rgb565(const void *src, int src_stride, int x, int y, int w, int h,
                           bool swap_bytes)
{
    if (s_panel == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (src == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > LCD_H_RES) { w = LCD_H_RES - x; }
    if (y + h > LCD_V_RES) { h = LCD_V_RES - y; }
    if (w <= 0 || h <= 0) {
        return ESP_OK;
    }

    /*
     * SPI 的 DMA 缓冲要在内部 RAM，而图像源在 PSRAM，
     * 所以逐批拷贝到内部缓冲再发送。
     * 注意: 图像源可能带行距 (stride)，不能直接整块 memcpy。
     */
    size_t buf_len = (size_t)w * LCD_BATCH_LINES * sizeof(uint16_t);
    uint16_t *buf = heap_caps_malloc(buf_len, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    if (buf == NULL) {
        return ESP_ERR_NO_MEM;
    }

    const uint8_t *src8 = (const uint8_t *)src;
    size_t row_bytes = (size_t)w * sizeof(uint16_t);

    esp_err_t err = ESP_OK;
    int pending = 0;   /* 已发出但还没收到回调的批次数 */

    xSemaphoreTake(s_lcd_mutex, portMAX_DELAY);
    for (int row = 0; row < h && err == ESP_OK; row += LCD_BATCH_LINES) {
        int lines = (h - row < LCD_BATCH_LINES) ? (h - row) : LCD_BATCH_LINES;

        /*
         * 关键: 等上一批颜色数据真正传完, 再覆写 buf。
         * SPI 颜色数据是异步 DMA 传输的, 不等就会把正在传的数据冲掉,
         * 画面被切成一条一条的横条 (每条 40 行 = 一个批次)。
         * take 一次 = 消耗一次 give, 收支平衡, 不会死锁。
         */
        if (pending > 0) {
            xSemaphoreTake(s_lcd_dma_done, portMAX_DELAY);
            pending--;
        }

        for (int l = 0; l < lines; l++) {
            const uint8_t *srow = src8 + (size_t)(row + l) * src_stride;
            uint16_t *drow = &buf[(size_t)l * w];

            if (swap_bytes) {
                /* 源是大端 (高位在前)，面板按小端解读，逐像素交换高低字节 */
                for (int i = 0; i < w; i++) {
                    drow[i] = (uint16_t)((srow[i * 2] << 8) | srow[i * 2 + 1]);
                }
            } else {
                memcpy(drow, srow, row_bytes);
            }
        }

        /* x_end / y_end 是【开区间】右边界 */
        err = esp_lcd_panel_draw_bitmap(s_panel, x, y + row, x + w, y + row + lines, buf);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "draw_rgb565 失败 @(%d,%d): %s", x, y + row, esp_err_to_name(err));
        } else {
            pending++;
        }
    }

    /* 最后一批也要等传完才能 free(buf) */
    while (pending > 0) {
        xSemaphoreTake(s_lcd_dma_done, portMAX_DELAY);
        pending--;
    }
    xSemaphoreGive(s_lcd_mutex);
    free(buf);
    return err;
}

/* ---------------- 测试图样 (定位花屏用) ---------------- */

/*
 * 画一张固定图样, 完全不依赖摄像头。
 *
 * 用途: 把 "LCD 传输链路" 和 "摄像头数据" 两个嫌疑分开。
 *   - 图样稳定不闪 -> LCD/SPI 链路 OK, 问题在摄像头数据侧 (帧缓冲/字节序/PSRAM DMA)
 *   - 图样也闪     -> 问题在 LCD/SPI 链路本身 (DC 时序/缓冲复用/批次边界)
 *
 * 图样内容 (竖条, 从左到右):
 *   0-7:   标准 RGB565 原色条 (红 绿 蓝 黄 青 品红 白 黑)
 *   8-15:  8 级灰阶 (黑->白), 用来检查亮度线性
 *   下半屏: 棋盘格 (黑白交替 8x8), 用来检查像素错位
 */
esp_err_t lcd_draw_test_pattern(void)
{
    if (s_panel == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    static const uint16_t color_bars[8] = {
        0xF800, /* 红 */
        0x07E0, /* 绿 */
        0x001F, /* 蓝 */
        0xFFE0, /* 黄 */
        0x07FF, /* 青 */
        0xF81F, /* 品红 */
        0xFFFF, /* 白 */
        0x0000, /* 黑 */
    };

    const int bar_w = LCD_H_RES / 16;   /* 每条宽度 */
    int x = 0;

    /* ---- 8 条原色 ---- */
    for (int i = 0; i < 8; i++) {
        esp_err_t err = lcd_fill_rect(x, 0, bar_w, LCD_V_RES / 2, color_bars[i]);
        if (err != ESP_OK) {
            return err;
        }
        x += bar_w;
    }

    /* ---- 8 级灰阶 ---- */
    for (int i = 0; i < 8; i++) {
        uint16_t v = (uint16_t)(i * 0x21);   /* 0,0x21,...,0xE7 */
        uint16_t gray = (uint16_t)((v << 11) | (v << 5) | v);
        esp_err_t err = lcd_fill_rect(x, 0, bar_w, LCD_V_RES / 2, gray);
        if (err != ESP_OK) {
            return err;
        }
        x += bar_w;
    }

    /* ---- 下半屏: 棋盘格 (8x8 像素) ---- */
    for (int cy = 0; cy < LCD_V_RES / 2; cy += 8) {
        for (int cx = 0; cx < LCD_H_RES; cx += 8) {
            bool white = (((cx / 8) + (cy / 8)) & 1) == 0;
            uint16_t c = white ? 0xFFFF : 0x0000;
            esp_err_t err = lcd_fill_rect(cx, LCD_V_RES / 2 + cy, 8, 8, c);
            if (err != ESP_OK) {
                return err;
            }
        }
    }

    ESP_LOGI(TAG, "测试图样已绘制 (色条 + 灰阶 + 棋盘格)");
    return ESP_OK;
}

/* ---------------- 合成图像测试 (定位花屏用) ---------------- */

/*
 * 造一张 320x240 的渐变图放在 PSRAM 里, 再走 lcd_draw_rgb565 画上去。
 *
 * 为什么需要这一步:
 *   自检图样稳定 -> LCD/SPI 链路 OK。但自检走的是 lcd_fill_rect(纯色),
 *   摄像头走的是 lcd_draw_rgb565(逐像素换字节 + PSRAM 源 + 分批)。
 *   用合成图走同一条路径, 就能把 "draw_rgb565 代码路径" 和
 *   "摄像头帧数据" 这两个嫌疑彻底分开。
 */
esp_err_t lcd_draw_synthetic_image(bool swap_bytes)
{
    if (s_panel == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    /* 故意放在 PSRAM, 和摄像头帧缓冲同处, 复现同样的内存条件 */
    uint8_t *img = heap_caps_malloc(LCD_H_RES * LCD_V_RES * 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (img == NULL) {
        ESP_LOGE(TAG, "合成图像 PSRAM 分配失败");
        return ESP_ERR_NO_MEM;
    }

    /*
     * 造内容: 每行是水平渐变 + 行号色带, 这样任何错位/撕裂都能一眼看出来。
     * 存成"大端"(高位字节在前), 和摄像头帧缓冲的格式保持一致。
     */
    for (int y = 0; y < LCD_V_RES; y++) {
        uint8_t *row = img + (size_t)y * LCD_H_RES * 2;
        for (int x = 0; x < LCD_H_RES; x++) {
            uint16_t r = (uint16_t)(x * 31 / LCD_H_RES);          /* 0..31 */
            uint16_t g = (uint16_t)(y * 31 / LCD_V_RES);          /* 0..31 */
            uint16_t b = (uint16_t)((x + y) & 0x1F);              /* 0..31 */
            uint16_t c = (uint16_t)((r << 11) | (g << 5) | b);
            row[x * 2]     = (uint8_t)(c >> 8);   /* 高位在前 = 大端 */
            row[x * 2 + 1] = (uint8_t)(c & 0xFF);
        }
    }

    esp_err_t err = lcd_draw_rgb565(img, LCD_H_RES * 2, 0, 0,
                                    LCD_H_RES, LCD_V_RES, swap_bytes);
    free(img);
    return err;
}

/* ---------------- 摄像头帧缓冲诊断 ---------------- */

/*
 * 上半屏: 直接画 PSRAM 里的摄像头原始帧缓冲
 * 下半屏: 先整帧拷到内部 RAM, 再画那份副本
 *
 * 两者都由串口日志里的校验和来佐证, 不需要人眼看屏幕也能下结论。
 */
esp_err_t lcd_draw_frame_diagnostic(const void *src, int src_stride, int w, int h,
                                     bool swap_bytes, uint8_t *copy_buf)
{
    if (s_panel == NULL || src == NULL || copy_buf == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 先整帧拷到内部 RAM —— 这一步顺便验证 PSRAM 读是否正常 */
    const uint8_t *src8 = (const uint8_t *)src;
    for (int y = 0; y < h; y++) {
        memcpy(copy_buf + (size_t)y * w * 2, src8 + (size_t)y * src_stride, (size_t)w * 2);
    }

    /* 上半屏: 画原始源 (PSRAM), 只画上面 120 行 */
    int half_h = h / 2;
    esp_err_t err = lcd_draw_rgb565(src, src_stride, 0, 0, w, half_h, swap_bytes);
    if (err != ESP_OK) {
        return err;
    }

    /* 下半屏: 画内部 RAM 副本 */
    err = lcd_draw_rgb565(copy_buf, w * 2, 0, half_h, w, h - half_h, swap_bytes);
    return err;
}

/*
 * 四象限花屏定位图
 *
 * 把屏幕分成 4 块, 每块用不同的"处理方式"画同一帧数据。
 * 一眼就能看出问题出在哪一环:
 *
 *   +----------------+----------------+
 *   | 左上: 原样      | 右上: 交换字节  |   <- 直接画 PSRAM 源
 *   | (大端, 不交换)  | (大端->小端)    |
 * +----------------+----------------+
 *   | 左下: 内部RAM   | 右下: 合成渐变  |   <- 验证 RAM 拷贝 + 画图代码
 *   | (拷贝后不交换)  | (程序生成, 非摄像头)|
 * +----------------+----------------+
 *
 * 判读:
 *   - 右下 (合成图) 也花  -> LCD/SPI 链路本身坏了 (和摄像头无关)
 *   - 右下正常, 左上/右上花 -> 摄像头帧数据是垃圾
 *   - 左上花但左下正常   -> PSRAM 读取/cache 同步问题
 *   - 全都正常           -> 之前的"花"其实是显示时序问题, 不是数据问题
 */
esp_err_t lcd_draw_quad_diagnostic(const void *src, int src_stride, int w, int h,
                                   bool swap_bytes, uint8_t *copy_buf)
{
    if (s_panel == NULL || src == NULL || copy_buf == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    const uint8_t *src8 = (const uint8_t *)src;

    const int qw = w / 2;
    const int qh = h / 2;

    /*
     * 【关键】四个象限不是"四种处理方式", 而是【整帧的四个分块】:
     *
     *   +------------------+------------------+
     *   | Q1: 左上 1/4     | Q2: 右上 1/4     |
     *   |   源像素(0,0)    |   源像素(160,0)  |
     * +------------------+------------------+
     *   | Q3: 左下 1/4     | Q4: 右下 1/4     |
     *   |   源像素(0,120)  |   源像素(160,120)|
     *   +------------------+------------------+
     *
     * 这样一屏就把【整个 320x240 帧】都显示出来了, 任何一块有问题立刻可见。
     *
     * 上一版只画了源的左上 160x120, 帧的其余 3/4 根本没被检验过,
     * 所以"左上好"只能说明左上角那一小块好, 不能说明整帧好。
     */
    const uint8_t *s8 = (const uint8_t *)src;

    /* Q1: 源 (0,0) 160x120 */
    esp_err_t err = lcd_draw_rgb565(s8, src_stride, 0, 0, qw, qh, swap_bytes);
    if (err != ESP_OK) {
        return err;
    }

    /* Q2: 源 (160,0) 160x120 */
    err = lcd_draw_rgb565(s8 + (size_t)qw * 2, src_stride, qw, 0, w - qw, qh, swap_bytes);
    if (err != ESP_OK) {
        return err;
    }

    /* Q3: 源 (0,120) 160x120 */
    err = lcd_draw_rgb565(s8 + (size_t)qh * src_stride, src_stride, 0, qh, qw, h - qh, swap_bytes);
    if (err != ESP_OK) {
        return err;
    }

    /* Q4: 源 (160,120) 160x120 */
    err = lcd_draw_rgb565(s8 + (size_t)qh * src_stride + (size_t)qw * 2, src_stride,
                          qw, qh, w - qw, h - qh, swap_bytes);
    if (err != ESP_OK) {
        return err;
    }

    return ESP_OK;
}
