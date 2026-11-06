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
static esp_lcd_panel_io_handle_t s_io = NULL;   /* 保存 io 句柄, 用于运行时重建面板 */
static int s_lcd_spi_hz = 60 * 1000 * 1000;     /* 当前 SPI 时钟, 重建时用 */

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
 *
 * 【2026-11-05】进一步降到 10 行 (6400 字节/块, 两块共 12800 字节)。
 * 原因不是 SPI 边界, 而是【内部 RAM 不够】:
 * 这块板子开机实测内部 RAM 只剩 16983 字节, 两块 12800 字节的缓冲
 * 直接申请失败 -> 屏幕全黑。降到 10 行后占用减半, 才有余量。
 * 批次变成 24 批, 但每批只有 6400 字节, 总传输量不变, 帧率基本不受影响。
 */
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

/*
 * 【2026-11-05 修复黑屏】常驻的双缓冲, 不再每帧 malloc/free。
 *
 * ---------------------------------------------------------------------------
 * 症状: 屏幕全黑, 串口每秒刷几十条:
 *     E lcd: LCD 双缓冲分配失败
 *     W kanhu: LCD 直绘失败: ESP_ERR_NO_MEM
 *
 * 根因: lcd_draw_rgb565() 每画一帧就 malloc 2 块 12800 字节的内部 DMA 缓冲,
 *       画完再 free。开机实测内部 RAM 只剩 16983 字节, 而双缓冲需要
 *       2 x 12800 = 25600 字节 —— 【根本不够】, 所以每一帧都申请失败,
 *       屏幕从来没有被成功画过一张, 自然全黑。
 *
 * 为什么之前能显示:
 *   之前的固件里有一块【从来没被用过】的诊断副本缓冲 (38400 字节内部 RAM),
 *   加上其他零散分配, 内部 RAM 一直很紧。这个 bug 被别的内存问题掩盖着。
 *
 * 修复:
 *   1) 缓冲改为【模块级常驻】, 只在 lcd_init() 里分配一次, 永远不释放。
 *      这样内部 RAM 的占用是【固定的、可预算的】, 不会因为某一帧
 *      内存紧张就整屏画不出来。
 *   2) 批量行数从 20 降到 10, 每块缓冲 6400 字节, 两块共 12800 字节,
 *      给 Wi-Fi / 音频 / 摄像头留出余量。
 *   3) 分配失败时【不清屏也不报错刷屏】, 只跳过这一帧 —— 内存紧张是
 *      常态, 刷屏会把系统拖垮, 而跳过一帧人眼根本看不出来。
 * ---------------------------------------------------------------------------
 */
#define LCD_BATCH_LINES  10   /* 每批 10 行 -> 320*10*2 = 6400 字节/块 */
#define LCD_BUF_COUNT    2     /* 双缓冲 */
#define LCD_BUF_BYTES    ((size_t)LCD_WIDTH * LCD_BATCH_LINES * sizeof(uint16_t))

static uint16_t *s_lcd_bufs[LCD_BUF_COUNT] = { NULL };
static bool     s_lcd_buf_ready = false;

/* SPI 传完一批颜色数据时回调 (在中断上下文触发) */
/*
 * SPI 一批颜色数据传完时的回调。
 *
 * ---------------------------------------------------------------------------
 * 【关键修复】这个回调运行在【中断上下文 (ISR)】!
 *
 * IDF 源码链路:
 *   spi_intr()  (中断服务函数)
 *     -> spi_post_trans()
 *       -> dev->cfg.post_cb(cur_trans)          <- 我们的回调
 *         -> lcd_spi_post_trans_color_cb()
 *           -> on_color_trans_done()
 *
 * 之前的注释说"这个回调运行在 SPI 驱动任务的上下文, 是普通任务, 不是 ISR"——
 * 【这是错的】! 那是老版本 IDF 的行为, IDF v5.x 的 spi_post_trans 明确
 * 是在 spi_intr 里调用的 (esp_driver_spi/src/gpspi/spi_master.c)。
 *
 * 在 ISR 里调用 xSemaphoreGive() (任务版) 是未定义行为:
 *   - 它会操作 FreeRTOS 的任务调度器状态, 在中断里做这件事会破坏内核状态
 *   - 实测表现: 信号量计数错乱 -> "DMA 还没传完" 的信号提前发出
 *     -> 下一批数据覆写了正在被 DMA 读取的 buf
 *     -> 面板收到新旧混合的数据 -> 画面被切成一条条横条 / 花屏
 *
 * 这与实测现象完全吻合:
 *   - 浏览器里的 MJPEG 画面【完全干净】(JPEG 编码器和 LCD 读同一个 fb->buf)
 *   - 只有 LCD 画面花
 *   => 摄像头数据没问题, 问题在 LCD 的 SPI 发送路径 -> 就是这个 buf 复用竞争
 *
 * 修复: 改用中断安全的 xSemaphoreGiveFromISR(),
 *       并在中断退出时请求一次任务调度 (portYIELD_FROM_ISR)。
 */
static bool lcd_color_trans_done_cb(esp_lcd_panel_io_handle_t panel_io,
                                    esp_lcd_panel_io_event_data_t *edata,
                                    void *user_ctx)
{
    BaseType_t higher_priority_task_woken = pdFALSE;
    if (s_lcd_dma_done != NULL) {
        xSemaphoreGiveFromISR(s_lcd_dma_done, &higher_priority_task_woken);
        if (higher_priority_task_woken == pdTRUE) {
            portYIELD_FROM_ISR();
        }
    }
    return false;
}

esp_err_t lcd_init(void)
{
    /*
     * 同步原语: 统计"已传完的批次数"。
     * 只在第一次创建 —— 运行时改 SPI 时钟会重建面板, 那时不能重建信号量,
     * 否则正在等待的绘制任务手里的信号量句柄就失效了。
     */
    if (s_lcd_dma_done == NULL) {
        s_lcd_dma_done = xSemaphoreCreateCounting(10, 0);
    }
    if (s_lcd_mutex == NULL) {
        s_lcd_mutex = xSemaphoreCreateMutex();
    }
    if (s_lcd_dma_done == NULL || s_lcd_mutex == NULL) {
        ESP_LOGE(TAG, "创建 LCD 同步信号量失败");
        return ESP_ERR_NO_MEM;
    }

    /*
     * 【2026-11-05 修复黑屏】常驻双缓冲, 在这里一次性分配。
     *
     * 之前是每帧 malloc/free, 内部 RAM 紧张时申请失败 -> 屏幕全黑。
     * 现在占用固定 (2 x 6400 = 12800 字节内部 DMA 内存), 可预算。
     *
     * 只分配一次: 运行时改 SPI 时钟会重建面板, 但缓冲不用重���。
     */
    if (!s_lcd_buf_ready) {
        for (int i = 0; i < LCD_BUF_COUNT; i++) {
            s_lcd_bufs[i] = heap_caps_malloc(LCD_BUF_BYTES,
                                             MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
            if (s_lcd_bufs[i] == NULL) {
                ESP_LOGE(TAG, "LCD 常驻缓冲分配失败 (%d 字节 x %d)",
                         (int)LCD_BUF_BYTES, LCD_BUF_COUNT);
                for (int j = 0; j < i; j++) {
                    free(s_lcd_bufs[j]);
                    s_lcd_bufs[j] = NULL;
                }
                return ESP_ERR_NO_MEM;
            }
        }
        s_lcd_buf_ready = true;
        ESP_LOGI(TAG, "LCD 常驻缓冲就绪: %d 块 x %d 字节 (内部DMA)",
                 LCD_BUF_COUNT, (int)LCD_BUF_BYTES);
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
        .pclk_hz                 = s_lcd_spi_hz,
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

    /*
     * 记住 io 句柄, 这样运行时可以重建面板来改 SCLK 频率 (A/B 定位用)。
     *
     * 背景: 官方正点原子例子用 60MHz, 但那是给他们的板子 (走 LCD 自己的
     * 时钟线) 调的。BOX3 的排线和电源噪声条件下, 60MHz 有可能在长
     * 传输时出现采样错误 -> 花屏。把频率降下来是很好的对照实验。
     *
     * IDF 5.3 没有 spi_device_set_clock, 只能删掉设备重新加。
     */
    s_io = io;

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

/*
 * 运行时调节 LCD 的 SPI 时钟频率 (A/B 对照用)。
 *
 * 用途: 官方用 60MHz; 如果在 BOX3 的排线/电源条件下 60MHz 会采样出错,
 *       降频后画面应该立刻变干净。用这个命令可以在不重新烧录的情况下
 *       快速定位"是不是 SPI 时钟太快"。
 *
 * IDF 5.3 没有 spi_device_set_clock(), 所以只能把面板整体重建:
 *   删掉 panel + io -> 重新 spi_bus_add_device (新频率) -> 重新 init。
 */
esp_err_t lcd_set_spi_clock(int hz)
{
    if (hz < 1000000 || hz > 80000000) {
        ESP_LOGE(TAG, "频率要在 1MHz~80MHz 之间");
        return ESP_ERR_INVALID_ARG;
    }
    if (s_panel == NULL || s_io == NULL) {
        ESP_LOGE(TAG, "LCD 还没初始化");
        return ESP_ERR_INVALID_STATE;
    }
    if (hz == s_lcd_spi_hz) {
        ESP_LOGI(TAG, "SPI 时钟已经是 %d Hz", hz);
        return ESP_OK;
    }

    /* 先停掉正在进行的传输, 避免删除设备时还有事务在飞 */
    xSemaphoreTake(s_lcd_mutex, portMAX_DELAY);
    while (xSemaphoreTake(s_lcd_dma_done, 0) == pdTRUE) {
        /* 清空计数 */
    }
    xSemaphoreGive(s_lcd_mutex);

    /* 删掉旧面板和 io */
    if (s_panel) {
        esp_lcd_panel_del(s_panel);
        s_panel = NULL;
    }
    if (s_io) {
        esp_lcd_panel_io_del(s_io);
        s_io = NULL;
    }
    /* 注意: esp_lcd_panel_io_del() 内部已经会 spi_bus_remove_device(),
     * 所以这里不能也不需要再手动移除 SPI 设备。 */

    s_lcd_spi_hz = hz;

    /* 重新初始化面板 (复用 lcd_init 的核心逻辑) */
    esp_err_t err = lcd_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "以 %d Hz 重建 LCD 失败: %s", hz, esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "LCD SPI 时钟已设为 %d Hz", hz);
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

    /*
     * 用【常驻缓冲】而不是每帧 malloc。
     *
     * 【2026-11-05】原来是 heap_caps_malloc(buf_len, MALLOC_CAP_DMA|INTERNAL),
     * 画完再 free。内部 RAM 紧张时这个申请会失败, 于是 lcd_fill_rect
     * 直接返回 ESP_ERR_NO_MEM, 屏幕上该区域永远是黑的。
     *
     * 常驻缓冲在 lcd_init() 里一次性分配, 占用固定可预算;
     * 这里假设 w <= LCD_WIDTH (调用方都满足), 所以缓冲一定够用。
     */
    if (!s_lcd_buf_ready) {
        return ESP_ERR_NO_MEM;
    }
    size_t need = (size_t)w * LCD_BATCH_LINES * sizeof(uint16_t);
    if (need > LCD_BUF_BYTES) {
        ESP_LOGE(TAG, "fill_rect 宽度 %d 超出常驻缓冲容量", w);
        return ESP_ERR_INVALID_ARG;
    }
    uint16_t *buf = s_lcd_bufs[0];

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

    /* 最后一批也要等传完才能复用 buf */
    while (pending > 0) {
        xSemaphoreTake(s_lcd_dma_done, portMAX_DELAY);
        pending--;
    }
    xSemaphoreGive(s_lcd_mutex);
    return err;
}

esp_err_t lcd_fill_screen(uint16_t color)
{
    return lcd_fill_rect(0, 0, LCD_H_RES, LCD_V_RES, color);
}

esp_err_t lcd_draw_rgb565(const void *src, int src_stride, int x, int y, int w, int h,
                           bool swap_bytes, bool yield_between_batches)
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
     *
     * ---------------------------------------------------------------------------
     * 【双缓冲】用两个缓冲交替, 而不是一块缓冲复用。
     *
     * 为什么必须双缓冲 (而不是靠信号量等上一批传完):
     *
     *   esp_lcd_panel_draw_bitmap() 内部做的事是:
     *     1) tx_param(CASET)  <- 里面会等队列里的事务传完
     *     2) tx_param(RASET)  <- 同上
     *     3) tx_color(RAMWR) <- 把 buf 丢进队列, 立刻返回 (DMA 后台在读它)
     *
     *   第 3 步是【异步】的: 函数返回时 DMA 还在读 buf。
     *   我们下一轮循环要往 buf 里写新的一批 —— 如果此时上一批还没传完,
     *   我们就把正在被 DMA 读取的内存覆盖了, 面板收到的是新旧混合数据,
     *   画面就变成一条条横条 / 花屏。
     *
     *   信号量能解决"等上一批传完", 但它在 ISR 里 give, 存在时序窗口:
     *   ISR 给出信号 -> 我们被唤醒 -> 但 DMA 可能还没完全结束。
     *   双缓冲彻底消除这个窗口: 写缓冲 B 时, DMA 读的是缓冲 A, 物理上不可能撞。
     *
     * 代价: 多一块 6400 字节的内部 RAM (LCD_BATCH_LINES=10 时)。
     *
     * ---------------------------------------------------------------------------
     * 【2026-11-05 修复黑屏】缓冲改成【模块级常驻】
     *
     * 原来是每帧 malloc 2 块 12800 字节的内部 DMA 缓冲, 画完 free。
     * 开机实测内部 RAM 只剩 16983 字节 < 25600, 所以【每一帧都申请失败】,
     * 屏幕从来没被成功画过一张 -> 全黑。
     *
     * 现在: 缓冲在 lcd_init() 里一次性分配, 常驻不释放。
     * 占用固定 (2 x 6400 = 12800 字节), 不会因为某一帧内存紧张就整屏失败。
     */
    if (!s_lcd_buf_ready) {
        /* 缓冲没就绪 -> 跳过这一帧, 不刷错误日志 (内存紧张是常态) */
        return ESP_ERR_NO_MEM;
    }
    size_t need = (size_t)w * LCD_BATCH_LINES * sizeof(uint16_t);
    if (need > LCD_BUF_BYTES) {
        ESP_LOGE(TAG, "draw_rgb565 宽度 %d 超出常驻缓冲容量", w);
        return ESP_ERR_INVALID_ARG;
    }
    uint16_t *bufs[LCD_BUF_COUNT] = { s_lcd_bufs[0], s_lcd_bufs[1] };

    const uint8_t *src8 = (const uint8_t *)src;
    size_t row_bytes = (size_t)w * sizeof(uint16_t);

    esp_err_t err = ESP_OK;
    int pending = 0;   /* 已发出但还没收到回调的批次数 */
    int cur = 0;       /* 当前正在写的缓冲下标 */
    int yield_counter = 0;  /* 让出 CPU 的计数器 */

    xSemaphoreTake(s_lcd_mutex, portMAX_DELAY);
    for (int row = 0; row < h && err == ESP_OK; row += LCD_BATCH_LINES) {
        int lines = (h - row < LCD_BATCH_LINES) ? (h - row) : LCD_BATCH_LINES;

        /*
         * 关键: 等上一批颜色数据真正传完, 再覆写缓冲。
         * 双缓冲下, 我们写的是 cur, DMA 读的是上一批的 1-cur, 所以
         * 即使不等也不会撞; 但这里仍然等一下, 保证不排队过多事务
         * (SPI 事务队列深度有限, 而且排队会延迟显示)。
         *
         * take 一次 = 消耗一次 give, 收支平衡, 不会死锁。
         */
        if (pending > 0) {
            xSemaphoreTake(s_lcd_dma_done, portMAX_DELAY);
            pending--;
        }

        uint16_t *buf = bufs[cur];
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
            cur ^= 1;   /* 下一批换到另一个缓冲 */
        }

        /*
         * 让出 CPU 给摄像头的 DVP DMA。
         *
         * 摄像头的 DVP 数据是通过 GDMA 搬进 PSRAM 的, 而 PSRAM 总线共享。
         * 如果画屏时长时间连续占用 PSRAM, DVP DMA 抢不到带宽 -> 丢行。
         * 所以每画完几批就让出一次。
         *
         * 让出频率: 每 8 批 (80 行) 让一次。
         *   之前是每批都让 (每批 10ms), 一帧要 120ms —— 太慢, 画面会有明显延迟感。
         *   每 8 批让一次的话一帧只要 ~30ms, 既让出了总线, 又不拖慢刷新。
         *
         * 【2026-11-05】批量行数从 20 降到 10 (为了省内部 RAM) 后,
         * 一帧的批次数从 12 变成 24。如果让出频率还是每 4 批,
         * 一帧就要让出 6 次 x 10ms = 60ms —— 实测 lcd_draw_us 确实到了 58ms,
         * 画面明显变卡。这里同步改成每 8 批, 让出次数减半, 又回到 ~30ms。
         * 让出的【总行数】不变 (还是每 80 行让一次), 所以对 DVP DMA 的效果不变。
         *
         * vTaskDelay(1) = 1 tick = 10ms (tick 率 100Hz)。
         * 注意不能用 pdMS_TO_TICKS(1) —— 那个在 100Hz 下算出来是 0, 等于没让。
         */
        if (yield_between_batches) {
            if ((++yield_counter % 8) == 0) {
                vTaskDelay(1);
            }
        }
    }

    /* 最后一批也要等传完才能复用 buf */
    while (pending > 0) {
        xSemaphoreTake(s_lcd_dma_done, portMAX_DELAY);
        pending--;
    }
    xSemaphoreGive(s_lcd_mutex);
    /* 【2026-11-05】缓冲常驻, 不再 free */
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
                                    LCD_H_RES, LCD_V_RES, swap_bytes, true);
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
    esp_err_t err = lcd_draw_rgb565(src, src_stride, 0, 0, w, half_h, swap_bytes, true);
    if (err != ESP_OK) {
        return err;
    }

    /* 下半屏: 画内部 RAM 副本 */
    err = lcd_draw_rgb565(copy_buf, w * 2, 0, half_h, w, h - half_h, swap_bytes, true);
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
    esp_err_t err = lcd_draw_rgb565(s8, src_stride, 0, 0, qw, qh, swap_bytes, true);
    if (err != ESP_OK) {
        return err;
    }

    /* Q2: 源 (160,0) 160x120 */
    err = lcd_draw_rgb565(s8 + (size_t)qw * 2, src_stride, qw, 0, w - qw, qh, swap_bytes, true);
    if (err != ESP_OK) {
        return err;
    }

    /* Q3: 源 (0,120) 160x120 */
    err = lcd_draw_rgb565(s8 + (size_t)qh * src_stride, src_stride, 0, qh, qw, h - qh, swap_bytes, true);
    if (err != ESP_OK) {
        return err;
    }

    /* Q4: 源 (160,120) 160x120 */
    err = lcd_draw_rgb565(s8 + (size_t)qh * src_stride + (size_t)qw * 2, src_stride,
                          qw, qh, w - qw, h - qh, swap_bytes, true);
    if (err != ESP_OK) {
        return err;
    }

    return ESP_OK;
}
