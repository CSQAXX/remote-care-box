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
/* MSG_PEEK 零副作用探测客户端断开 (见 stream_client_alive) */
#include <sys/select.h>
#include <sys/socket.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"

#include "esp_wifi.h"
#include "esp_http_client.h"
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

/*
 * HAL 层的 I2C 低层接口。
 *
 * 需要它是因为 IDF 提供了两个精确解决"总线卡死"的硬件功能:
 *   i2c_ll_master_fsm_rst()  —— 复位卡在等 ACK 的主控状态机
 *   i2c_ll_master_clr_bus()  —— 硬件自动打时钟脉冲, 直到从机释放 SDA
 * 这两个是 ESP32-S3 的 I2C 外设原生能力, 比用 GPIO 手动打脉冲可靠得多
 * (GPIO 手动打脉冲的前提是引脚没被外设的 GPIO matrix 接管, 而实际做不到)。
 */
#include "hal/i2c_ll.h"
#include "soc/soc_caps.h"
#include "driver/i2c.h"
#include "driver/usb_serial_jtag.h"

#include "img_converters.h"
#include "jpeg_yield.h"
#include "camera_pins.h"
#include "aw9523b.h"
#include "lcd_st7789.h"
#include "audio_board.h"
#include "g711.h"
#include "activity_detect.h"
#include "time_sync.h"
#include "lcd_prompt.h"
#include "push_client.h"
#include "reliability.h"
#include "power_board.h"
#include "ai_client.h"
#include "wake_word.h"
#include "voice_mode.h"
#include "wifi_config.h"

#include <math.h>

static const char *TAG = "kanhu";

#define HTTP_PORT       8080

/*
 * 【2026-11-06】控制面端口 —— 数据面/控制面分离。
 *
 * 8080 数据面: /stream /raw /audio/alaw /audio/down  (长连接, 允许被占满)
 * 8081 控制面: /health /activity /event /notify /cmd  (短请求, 独立 server)
 *
 * 为什么要分: IDF 的 httpd 客户端 socket 槽位被硬限制在
 * max_open_sockets(最多7) - 内部占3 = 4 个。/stream 一个客户端就长期占住
 * 1 个 socket + 1 个 worker。几个客户端一连上, 同端口的 /health 就全部
 * 排队 -> 表现为"板子 HTTP 挂了", 但相机其实还在正常跑 (实测复现)。
 *
 * ⚠️ 关键: IDF 的 httpd 用一个【固定端口的 ctrl socket】(默认 32768)
 *    做内部事件通知, 两个实例如果用同一个 ctrl_port 就会
 *    EADDRINUSE (errno 112) 导致第二个实例起不来。
 *    httpd_config_t.ctrl_port 就是为这个准备的 —— 必须给第二个实例
 *    配一个不同的 ctrl_port 才能共存。详见 httpd_control_start()。
 */
#define HTTP_CONTROL_PORT 8081
#define HTTP_CONTROL_CTRL_PORT 32769   /* 控制面实例的 ctrl socket 端口 */

/*
 * Wi-Fi 总开关。必须定义在文件靠前的位置 —— audio_selftest_loop()
 * 里有 #if WIFI_ENABLE 的编译期分支, 定义晚了那里会当成 0。
 *
 * 0 = 完全不开 WiFi (纯本地显示, 最干净的对照组)
 * 1 = 正常开 WiFi
 */
#ifndef WIFI_ENABLE
#define WIFI_ENABLE 1
#endif

/*
 * JPEG 质量: 1-100, 越大越清晰 (jpge / libjpeg 的语义)。
 *
 * 之前这里写的 "0-63, 越小越清晰" 是 OpenCV 的约定, 用在这里是错的:
 * 传 12 其实是【最低质量】, 画面被压得又糊又小块。
 * 看护场景要能分清人有没有摔倒/在不在床上, 建议 60-75。
 * 质量越高 -> JPEG 越大 -> 编码越慢 -> 帧率越低, 需要和帧率一起权衡。
 */
#define JPEG_QUALITY    65

/*
 * FreeRTOS tick 率 = 100Hz (CONFIG_FREERTOS_HZ=100), 也就是 1 tick = 10ms。
 *
 * 陷阱: pdMS_TO_TICKS(1) 在 tick 率 100Hz 下会被算成【0 tick】!
 *       (1 * 100 / 1000 = 0), 于是 vTaskDelay(0) 退化成纯 taskYIELD(),
 *       根本不会真正阻塞 —— 让出点等于没加。
 *
 * 所以这里直接用 tick 数 (vTaskDelay(1) = 10ms),
 * 并且用编译期断言防止以后有人改错。
 */
#define APP_TICK_HZ      100
#define APP_TICKS_PER_MS (APP_TICK_HZ / 1000)   /* 0 —— 说明 tick 率 < 1000 */
#define APP_DELAY_TICKS  1                      /* = 10ms */

/* 校验: 我们依赖 1 tick = 10ms 这个前提 */
_Static_assert(APP_TICK_HZ == 100, "本固件假设 FreeRTOS tick 率是 100Hz (1 tick = 10ms)");

/*
 * JPEG 编码时每隔多少行让出一次 CPU。
 *
 * 背景: esp32-camera 的 fmt2jpg_cb() 编码时完全不让出 CPU (约 69ms),
 *       会饿死 core0 的 IDLE0 -> 看门狗超时 -> 板子反复重启,
 *       同时饿死 LCD 任务 -> 屏幕卡死 (看着像闪花屏)。
 *
 * 32 行 = 每 32 行阻塞 1 tick (10ms), 240 行共让出 7 次 = 额外 70ms。
 * 编码耗时从 69ms 变成约 140ms (约 7fps), 换来稳定不重启 + LCD 不卡。
 *
 * 注意: 这是【有客户端拉流时】才走的路径。没人看视频流时
 *       根本不编码, 完全没有这个开销。
 */
#define JPEG_YIELD_EVERY_ROWS  32

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

/* ---------------- 音频调试开关 ----------------
 *
 * AUDIO_SELFTONE = 1: 开机自动循环播 440Hz 测试音 (3 秒一轮),
 *   用来确认喇叭链路通不通。已实测通过 (麦克风能拾到回授,
 *   rms 随响/停周期跳动), 现在置 0 恢复正常状态。
 *   需要复测时改成 1 重新烧录即可。
 */
#define AUDIO_SELFTONE      0

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
static volatile int         s_long_conns = 0;   /* 长连接总配额 (视频+音频) */
static uint32_t             s_frame_count = 0;

/*
 * 【2026-11-06】推流并发上限。
 *
 * IDF 的 http_server max_open_sockets 硬上限 7 (内部占 3, 客户端最多 4),
 * 每个活跃请求占 1 socket + 1 worker。留 2 个给 /health /activity /event
 * 这些短请求 —— "收告警"是刚需, "看画面"可以等。
 */
#define STREAM_MAX_CLIENTS 2

/*
 * 【2026-11-06】长连接总配额 (视频 + 音频 共享, 全局计数)。
 *
 * 背景: 控制面已经拆到独立端口 8081 (见 httpd_control_start()), 所以
 * 8080 上的长连接不再会挤掉 /health —— 短请求走控制面, 长连接走数据面。
 *
 * 但这个计数器是【全局】的 (两个 server 共享), 而音频端点同时注册在
 * 数据面和控制面, 所以这里设 4: 允许"数据面 2 路视频 + 控制面 1 路音频
 * 上行 + 控制面 1 路其它长连接"。真正的兜底还是各 server 自己的
 * socket 槽位 (数据面 4, 控制面 6)。
 */
#define LONG_CONN_MAX 4

/*
 * 长连接配额管理。
 *
 * 取配额: 返回 true 表示拿到, false 表示超限 (调用方回 503)。
 * 释放配额: 必须在长连接结束时调用 (所有退出路径都要覆盖)。
 */
static bool long_conn_acquire(void)
{
    if (s_long_conns >= LONG_CONN_MAX) {
        return false;
    }
    s_long_conns++;
    return true;
}

static void long_conn_release(void)
{
    if (s_long_conns > 0) {
        s_long_conns--;
    }
}

/* ---------------- 帧槽位 (生产者 / 消费者) ---------------- */

/* 前置声明: 编码函数定义在生产者任务之后 */
static size_t encode_jpeg_to_slot(const uint8_t *rgb565, int w, int h, uint8_t *dst);

/*
 * 应用任务统一绑定的 CPU 核。
 *
 * 这是修【闪花屏】的关键一环, 原理:
 *
 *   ESP32-S3 的 PSRAM 数据缓存是【每核独立】的
 *   (IDF 源码 cache_ll.h 里写得很清楚:
 *    "Both cores have their own DBUS0" / "Both cores have their own IBUS0")
 *   也就是说 core 0 和 core 1 各有一份 PSRAM 缓存副本。
 *
 *   esp32-camera 的 cam_task 被 CONFIG_CAMERA_CORE0=y 钉在 core 0,
 *   它取到帧之后会调用 cam_drop_psram_cache() 失效缓存 ——
 *   但那个操作【只对当前核(core 0)有效】!
 *
 *   如果我们的任务跑在 core 1 上:
 *     core 1 的缓存里还留着【上一次】读到的 PSRAM 旧数据
 *     -> 我们读到的是陈旧/撕裂的帧 -> 屏幕花屏
 *
 *   这正好解释了之前的现象:
 *     【浏览器 MJPEG 干净, 但 LCD 花屏】
 *     因为 httpd 任务和 lcd 任务被调度到了不同的核。
 *
 * 修复: 把所有碰 PSRAM 帧数据的任务全部钉到 core 0,
 *       保证它们和 cam_task 看到同一份缓存。
 *
 * 【2026-11-03】这个常量现在定义在 audio_board.h 里 (音频环形缓冲也在
 * PSRAM, 有同样的跨核缓存一致性问题), main.c 和 audio_board.c 共用一份,
 * 免得两边写成不同的核号。
 */
#ifndef APP_CORE_ID
#define APP_CORE_ID        0
#endif

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
 * 【已删除】四象限诊断用的内部 RAM 副本缓冲 (160x120x2 = 38400 字节)。
 *
 * 这个缓冲 2026-11-05 被删掉了, 原因:
 *   1. 它【从来没被用过】—— 全代码里只有分配语句, 没有任何读写。
 *   2. 它白白占掉 38400 字节内部 RAM, 而内部 RAM 在这块板子上极其紧张:
 *      开机实测只剩 16983 字节, 结果 LCD 双缓冲 (需要 25600 字节)
 *      申请失败 -> 每帧画屏都失败 -> 【屏幕全黑】。
 *
 * 教训: 板子上"看起来不起眼"的常驻缓冲, 每一个都会挤掉别人的内存。
 * 内部 RAM 的分配必须按"开机实测剩余量"倒推, 而不是凭感觉。
 */

/* 统计信息 (给 /health 看) */
static volatile uint32_t s_cam_frames = 0;      /* 摄像头取帧数 */
static volatile uint32_t s_jpg_frames = 0;      /* JPEG 编码成功数 */
static volatile uint32_t s_jpg_bytes = 0;       /* 最近一帧 JPEG 大小 */
static volatile uint32_t s_jpg_encode_us = 0;   /* 最近一帧 JPEG 编码耗时 */
static volatile uint32_t s_cam_get_us = 0;      /* 最近一次取帧耗时 */
static volatile uint32_t s_rgb_copy_us = 0;     /* 最近一次 RGB565 拷贝耗时 */
static volatile uint32_t s_torn_frames = 0;     /* 拷贝过程中被 DMA 改写的帧数 */
static volatile uint32_t s_clean_frames = 0;    /* 拷贝期间保持完好的帧数 */
static volatile uint32_t s_lcd_direct_frames = 0; /* 直绘模式的帧数 */
static volatile uint32_t s_lcd_draw_us = 0;      /* 最近一次直绘耗时 */

/*
 * 【花屏量化指标】—— 固件侧直接算出"这帧有多脏"。
 *
 * 背景: 之前一直在电脑上分析 JPEG 找花屏, 但 JPEG 会把噪点平滑掉,
 * 而且 /raw 抓帧还被一个开关 gate 住 (抓到的其实是旧帧),
 * 导致分析结论全部作废。现在把这个指标做进固件, 任何时候
 * 串口发一条命令就能看到数字, 不用猜、不用人眼看屏幕。
 *
 * 指标定义 (对原始 RGB565, 采样 30 行):
 *   hi_energy / lo_energy = 相邻像素平均绝对差 (0~255)
 *       干净画面 < 3; 花屏时几十甚至上百。
 *   hi_jumps / lo_jumps = 相邻差 > 16 的像素对个数
 *   bad_rows = 能量超过阈值 (NOISE_BAD_ENERGY) 的行数
 *
 * 关键判据: 同一个场景下, 数值【越小越干净】。
 */
static volatile uint32_t s_hi_energy = 0;   /* 高字节相邻差均值 * 100 */
static volatile uint32_t s_lo_energy = 0;   /* 低字节相邻差均值 * 100 */
static volatile uint32_t s_hi_jumps = 0;
static volatile uint32_t s_lo_jumps = 0;
static volatile uint32_t s_bad_rows = 0;    /* 脏行数 (0~30) */
static volatile uint32_t s_qual_frames = 0; /* 参与统计的帧数 */

/*
 * 【逐通道质量指标】—— 这才是能真正反映"花屏"的指标。
 *
 * 为什么旧的 hi/lo_energy 指标是废的:
 *   旧指标直接算【原始字节】的相邻差。RGB565 的两个字节各自承载
 *   不同位域:
 *     高字节 = R5(bit15..11) | G3(bit10..8)
 *     低字节 = G2(bit7..5)   | B5(bit4..0)
 *   两个像素相邻时, 只要绿色跳 1, 字节差就会突然出现 32 的台阶
 *   (因为 G2 跨过了 bit 边界), 于是"平均字节差"被这个位域边界效应
 *   主导 —— 实测低字节能量 53.9 里有 32 就是这么来的, 是伪像。
 *
 * 正确的做法: 先【按位域拆开】, 再在每个通道内部算相邻差。
 *
 * 拆分后实测 (raw3_1.bin):
 *     R5 (hi>>3)   相邻差均值 1.02   自相关 0.957  -> 干净
 *     G3 (hi&7)    相邻差均值 0.27   自相关 0.947  -> 干净
 *     G2 (lo>>5)   相邻差均值 1.57   自相关 0.391  -> 【随机噪声】
 *     B5 (lo&31)   相邻差均值 1.01   自相关 0.968  -> 干净
 *
 * 而 G2 (绿色的低 3 位) 正是 ESP32-S3 硬件 YUV->RGB 转换器输出的
 * 那个字段, 它每帧重新随机 (帧间自相关 0.02), 熵 3.00 bit (均匀分布),
 * 且与其它通道完全不相关 (corr(G2,R5) = -0.00)。
 * 这就是花屏的真正来源: 【转换器的绿色低位是垃圾】。
 *
 * 新指标 s_g2_bad_pct:
 *   统计【绿色低 3 位】相邻像素的"中值残差" > 3 的像素占比。
 *   干净画面 0.0%; 现在的花屏状态 11~20%。
 *   这是一个和"位域边界"无关的真指标, 可以直接用来判定修好没有。
 */
static volatile uint32_t s_g2_bad_pct = 0;   /* 绿色低位坏点占比 (0~10000 => 0.00~100.00%) */
static volatile uint32_t s_g2_energy = 0;    /* 绿色低位相邻差均值 * 100 */
static volatile uint32_t s_r5_energy = 0;   /* 红色 5 位相邻差均值 * 100 */
static volatile uint32_t s_b5_energy = 0;   /* 蓝色 5 位相邻差均值 * 100 */

/*
 * 【缓冲稳定性测试】—— 决定性实验: 帧缓冲会不会被 DMA 偷偷改写。
 *
 * 做法: 在生产者手里对同一块帧缓冲算两次哈希, 中间隔 250ms
 *       (这段时间摄像头已经转了 5~6 帧, 足够 DMA 覆盖这块缓冲)。
 *
 * 判读:
 *   两次哈希【不同】 -> 帧缓冲在我们【还拿着它的时候】就被 DMA 改写了。
 *                        这是"撕裂/花屏"的经典成因:
 *                        摄像头有 fb_count 个缓冲, GRAB_LATEST 模式下
 *                        队列里始终是最新帧, 我们拿到手的帧随时可能被
 *                        下一帧的 DMA 覆盖 —— 而我们正在把它画到屏幕上。
 *   两次哈希【相同】 -> 帧缓冲是稳定的, 噪声来自【数据本身】,
 *                        即传感器/DVP 链路的电气或格式问题。
 */
static volatile uint32_t s_hash_test_a = 0;
static volatile uint32_t s_hash_test_b = 0;
static volatile bool    s_hash_test_armed = false;

/* FNV-1a 32 位哈希, 快速且够用 */
static uint32_t buf_hash(const uint8_t *buf, size_t len)
{
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < len; i++) {
        h ^= buf[i];
        h *= 16777619u;
    }
    return h;
}

/*
 * 原始 RGB565 帧快照 —— 取证用。
 *
 * 把最近一帧的【原始 RGB565】原样存下来 (不做任何 JPEG 编码),
 * 通过 /raw 端点导出到电脑, 这样就能在电脑上逐字节分析:
 *   - 到底哪些字节被污染
 *   - 是整行错位还是逐像素跳变
 *   - 是不是"半个像素"的错位 (低字节被上一行的高字节顶掉)
 *
 * 之前只能看 JPEG, 而 JPEG 会把噪点平滑掉, 根本看不出真相。
 */
#define RAW_SNAPSHOT_BYTES (320 * 240 * 2)
static uint8_t *s_raw_snapshot = NULL;      /* PSRAM, 原始 RGB565 */
static volatile uint32_t s_raw_snapshot_seq = 0;   /* 每存一次 +1 */
static volatile uint32_t s_raw_snapshot_ready = 0; /* 存了几份 */

/* 供 measure_bytes 等使用的原始 RGB565 缓冲 */

/*
 * 原始 RGB565 帧的"噪点"统计 —— 定位花屏的第一手数字。
 *
 * 怎么算:
 *   摄像头帧是 RGB565 (大端)。相邻两个像素的差值取平均, 就是这一行的
 *   "纹理能量"。干净画面相邻像素差很小 (渐变), 被污染的行会有大量
 *   大跳变 -> 能量飙升。
 *
 *   报告两个指标:
 *     jumps  = 差值 > 阈值的像素对个数 (阈值取满量程的 1/16)
 *     energy = 相邻像素平均绝对差 (0~65535)
 *
 * 为什么要在固件里算 (而不是在电脑上算 JPEG):
 *   JPEG 编码会把噪点平滑掉, 看起来就不明显;
 *   而 LCD 显示的是【原始 RGB565】, 噪点会原形毕露。
 *
 * 自校准:
 *   同一套指标也用来测【程序生成的合成图】(一定是干净的)。
 *   拿两者对比, 就能确定噪声到底来自摄像头还是来自显示链路 ——
 *   不用靠肉眼猜。
 */
#define NOISE_PROBE_ROWS   30
#define NOISE_JUMP_THRESH  0x1000   /* RGB565 差值 > 4096 算一次大跳变 */

typedef struct {
    uint32_t jumps;    /* 大跳变个数 (累计) */
    uint32_t energy;   /* 相邻像素平均绝对差 (累计, 已除以像素对数) */
    uint32_t samples;  /* 采样了多少行 */
} noise_stat_t;

/*
 * 高/低字节分别测量 —— 定位污染到底落在哪一半字节上。
 *
 * RGB565 大端存放: 像素 = (hi << 8) | lo
 *   hi = R5 G3   (颜色信息的主要部分)
 *   lo = G2 B5   (颜色的低位)
 *
 * 如果噪点只污染 lo, 说明 DMA 写的是"半个像素"的错位;
 * 如果 hi 也脏, 说明整行都乱了。
 */
typedef struct {
    uint32_t hi_energy;   /* 高字节相邻差的平均 */
    uint32_t lo_energy;   /* 低字节相邻差的平均 */
    uint32_t hi_jumps;    /* 高字节大跳变数 */
    uint32_t lo_jumps;    /* 低字节大跳变数 */
} byte_stat_t;

static void measure_bytes(const uint8_t *buf, int width, int height, byte_stat_t *out)
{
    uint32_t hi_sum = 0, lo_sum = 0, hi_j = 0, lo_j = 0, rows = 0;

    int step = height / NOISE_PROBE_ROWS;
    if (step < 1) {
        step = 1;
    }

    for (int y = 0; y < height; y += step) {
        const uint8_t *row = buf + (size_t)y * width * 2;
        uint8_t ph = row[0], pl = row[1];
        uint32_t rs_h = 0, rs_l = 0, rj_h = 0, rj_l = 0;

        for (int x = 1; x < width; x++) {
            uint8_t h = row[x * 2];
            uint8_t l = row[x * 2 + 1];
            uint32_t dh = (h > ph) ? (h - ph) : (ph - h);
            uint32_t dl = (l > pl) ? (l - pl) : (pl - l);
            rs_h += dh;
            rs_l += dl;
            if (dh > 16) rj_h++;
            if (dl > 16) rj_l++;
            ph = h;
            pl = l;
        }

        hi_sum += rs_h / (uint32_t)(width - 1);
        lo_sum += rs_l / (uint32_t)(width - 1);
        hi_j += rj_h;
        lo_j += rj_l;
        rows++;
    }

    out->hi_energy = rows ? (hi_sum / rows) : 0;
    out->lo_energy = rows ? (lo_sum / rows) : 0;
    out->hi_jumps = hi_j;
    out->lo_jumps = lo_j;
}

static byte_stat_t s_bytes = {0, 0, 0, 0};

/*
 * 【新增】量化一帧的"脏程度" —— 固件侧指标, 用来客观判断花屏修没修好。
 *
 * 与 measure_bytes 的区别:
 *   measure_bytes 是累计型的 (统计跳变/能量), 给的是"这一帧有多少跳变";
 *   这里是"一行一个能量值", 顺便数出有多少行是脏的 (bad_rows),
 *   这样能区分:
 *     - 全屏均匀噪点  -> bad_rows 高
 *     - 局部花带      -> bad_rows 低但有值
 *
 * 阈值 NOISE_BAD_ENERGY 取 12 (相邻像素平均绝对差):
 *   干净的自然画面, 相邻像素差一般在 1~3;
 *   花屏的行会飙到 30 以上。
 */
#define NOISE_BAD_ENERGY  12

typedef struct {
    uint32_t hi_energy;   /* 高字节相邻差均值 * 100 */
    uint32_t lo_energy;   /* 低字节相邻差均值 * 100 */
    uint32_t hi_jumps;
    uint32_t lo_jumps;
    uint32_t bad_rows;
    uint32_t rows;
} quality_stat_t;

static void measure_quality(const uint8_t *buf, int width, int height,
                            quality_stat_t *out)
{
    uint32_t hi_sum = 0, lo_sum = 0, hi_j = 0, lo_j = 0, rows = 0, bad = 0;

    int step = height / NOISE_PROBE_ROWS;
    if (step < 1) {
        step = 1;
    }

    for (int y = 0; y < height; y += step) {
        const uint8_t *row = buf + (size_t)y * width * 2;
        uint8_t ph = row[0], pl = row[1];
        uint32_t rs_h = 0, rs_l = 0, rj_h = 0, rj_l = 0;

        for (int x = 1; x < width; x++) {
            uint8_t h = row[x * 2];
            uint8_t l = row[x * 2 + 1];
            uint32_t dh = (h > ph) ? (uint32_t)(h - ph) : (uint32_t)(ph - h);
            uint32_t dl = (l > pl) ? (uint32_t)(l - pl) : (uint32_t)(pl - l);
            rs_h += dh;
            rs_l += dl;
            if (dh > 16) rj_h++;
            if (dl > 16) rj_l++;
            ph = h;
            pl = l;
        }

        uint32_t eh = rs_h / (uint32_t)(width - 1);
        uint32_t el = rs_l / (uint32_t)(width - 1);
        hi_sum += eh;
        lo_sum += el;
        hi_j += rj_h;
        lo_j += rj_l;
        /* 一行只要高/低字节里有一个很脏, 就算脏行 */
        if (eh > NOISE_BAD_ENERGY || el > NOISE_BAD_ENERGY) {
            bad++;
        }
        rows++;
    }

    out->hi_energy = rows ? (hi_sum / rows) * 100 : 0;
    out->lo_energy = rows ? (lo_sum / rows) * 100 : 0;
    out->hi_jumps = hi_j;
    out->lo_jumps = lo_j;
    out->bad_rows = bad;
    out->rows = rows;
}

/*
 * 【逐通道质量指标】measure_channel_quality()
 *
 * 把 RGB565 按位域拆开, 在【每个通道内部】算相邻差和中值残差,
 * 彻底避开"跨位域边界"的伪像。
 *
 * 关注的是 G2 (绿色低 3 位, = 低字节的 bit7..5):
 *   ESP32-S3 硬件 YUV->RGB 转换器输出的绿色低位是垃圾,
 *   而人眼对绿色最敏感, 所以它单独一个字段就能毁掉整幅画面。
 *
 * 判定标准 (实测标定):
 *   干净画面   : g2_bad_pct < 1%     (实测 0.00%)
 *   当前花屏状态: g2_bad_pct 11~20%  (实测 11.48% / 12.68% / 15.75% / 19.42%)
 *
 * 中值残差 (而不是简单的相邻差) 更稳健:
 *   |G2[x] - median(G2[x-1], G2[x+1])| > 3
 * 对真正随机的 G2 会稳定命中, 对平滑渐变则完全不命中。
 */
#define CHAN_G2_BAD_THR   3    /* 中值残差阈值 (0~7) */

typedef struct {
    uint32_t g2_bad_pct;   /* 绿色低位坏点占比 * 100 (0~10000) */
    uint32_t g2_energy;    /* 绿色低位相邻差均值 * 100 */
    uint32_t r5_energy;    /* 红色 5 位相邻差均值 * 100 */
    uint32_t b5_energy;    /* 蓝色 5 位相邻差均值 * 100 */
} chan_stat_t;

static void measure_channel_quality(const uint8_t *buf, int width, int height,
                                    chan_stat_t *out)
{
    /* 采样若干行, 累加各通道的相邻差 */
    uint32_t g2_sum = 0, r5_sum = 0, b5_sum = 0, pairs = 0;
    uint32_t g2_bad = 0, total = 0;

    int step = height / NOISE_PROBE_ROWS;
    if (step < 1) {
        step = 1;
    }

    for (int y = 0; y < height; y += step) {
        const uint8_t *row = buf + (size_t)y * width * 2;

        uint8_t p_g2 = (uint8_t)((row[1] >> 5) & 7);
        uint8_t p_r5 = (uint8_t)((row[0] >> 3) & 0x1F);
        uint8_t p_b5 = (uint8_t)(row[1] & 0x1F);
        uint8_t prev_g2 = p_g2;

        for (int x = 1; x < width; x++) {
            const uint8_t *px = row + (size_t)x * 2;
            uint8_t g2 = (uint8_t)((px[1] >> 5) & 7);
            uint8_t r5 = (uint8_t)((px[0] >> 3) & 0x1F);
            uint8_t b5 = (uint8_t)(px[1] & 0x1F);

            g2_sum += (g2 > prev_g2) ? (g2 - prev_g2) : (prev_g2 - g2);
            r5_sum += (r5 > p_r5) ? (r5 - p_r5) : (p_r5 - r5);
            b5_sum += (b5 > p_b5) ? (b5 - p_b5) : (p_b5 - b5);

            /*
             * 中值残差: 用左右邻居的中值当"应该是多少",
             * 差得多就说明这一格是【离群点】(噪声), 而不是平滑过渡。
             *
             *   resid(x) = |G2[x] - median(G2[x-1], G2[x+1])|
             *
             * 对真正随机的 G2 会稳定命中, 对平滑渐变则完全不命中。
             */
            if (x >= 2 && x + 1 < width) {
                uint8_t left  = (uint8_t)((row[(size_t)(x - 1) * 2 + 1] >> 5) & 7);
                uint8_t right = (uint8_t)((row[(size_t)(x + 1) * 2 + 1] >> 5) & 7);
                uint8_t expect = (uint8_t)((left + right) / 2);
                uint8_t resid = (g2 > expect) ? (uint8_t)(g2 - expect)
                                              : (uint8_t)(expect - g2);
                if (resid > CHAN_G2_BAD_THR) {
                    g2_bad++;
                }
            }
            total++;

            prev_g2 = g2;
            p_r5 = r5;
            p_b5 = b5;
        }
        pairs += (uint32_t)(width - 1);
    }

    /*
     * 注意: 必须【先乘 100 再除】。
     * 如果写成 (g2_sum / pairs) * 100, 整数除法会先把均值截断成 0,
     * 再乘 100 还是 0 —— 均值小于 1 时就永远显示 0.00。
     */
    out->g2_energy = pairs ? (g2_sum * 100u) / pairs : 0;
    out->r5_energy = pairs ? (r5_sum * 100u) / pairs : 0;
    out->b5_energy = pairs ? (b5_sum * 100u) / pairs : 0;
    out->g2_bad_pct = total ? (g2_bad * 10000u) / total : 0;
}

/*
 * 【软件修复绿色低位】repair_green_low()
 *
 * 就地修掉 RGB565 里的 G2 字段 (低字节的 bit7..5 = 绿色的低 3 位)。
 *
 * 为什么这样做是合理的:
 *   实测 G3 (绿色高 3 位) 干净 (中值残差 > 3 只有 0.01%~1.9%),
 *   G2 (绿色低 3 位) 是随机噪声 (11%~20%)。
 *   也就是说: 我们【知道】每个像素的绿色落在哪个 8 分之一区间 (G3),
 *   但区间内的精确值 (G2) 是垃圾。
 *
 * 修法: 用【同一行左右邻居的中值】填回 G2。
 *   - 对真噪声: 邻居的中值几乎不可能等于这个随机值 -> 被替换掉;
 *   - 对真渐变: 邻居中值 ≈ 真实值 -> 几乎不改。
 *   所以这是【只降噪、不损伤细节】的操作。
 *
 * 注意 G3 与 G2 的关系: 真实的绿色 = (G3<<3) | G2, 落在
 * [G3*8, G3*8+7] 这个 8 步长的区间里。用邻居的中值时, 我们还要
 * 把中值夹到 G3 决定的区间内, 否则会破坏 G3 表达的量级。
 *
 * 逐行处理, 每行独立 (行的边界用行内邻居)。
 */
static void repair_green_low(uint8_t *buf, int width, int height)
{
    for (int y = 0; y < height; y++) {
        uint8_t *row = buf + (size_t)y * width * 2;

        /* 先把整行的 G2 取出来 (顺便记下 G3) */
        for (int x = 0; x < width; x++) {
            uint8_t *px = row + (size_t)x * 2;
            uint8_t g3 = (uint8_t)((px[0] >> 3) & 0x07);
            uint8_t g2 = (uint8_t)((px[1] >> 5) & 0x07);

            uint8_t new_g2 = g2;
            if (x >= 1 && x + 1 < width) {
                uint8_t l_g3 = (uint8_t)((row[(size_t)(x - 1) * 2] >> 3) & 0x07);
                uint8_t l_g2 = (uint8_t)((row[(size_t)(x - 1) * 2 + 1] >> 5) & 0x07);
                uint8_t r_g3 = (uint8_t)((row[(size_t)(x + 1) * 2] >> 3) & 0x07);
                uint8_t r_g2 = (uint8_t)((row[(size_t)(x + 1) * 2 + 1] >> 5) & 0x07);

                /* 邻居的低位, 先各自夹到各自 G3 的区间里 */
                uint8_t l_eff = (uint8_t)(l_g3 * 8 + l_g2);
                uint8_t r_eff = (uint8_t)(r_g3 * 8 + r_g2);
                uint8_t mid = (uint8_t)((l_eff + r_eff) / 2);

                /* 再夹回本像素 G3 决定的区间 [g3*8, g3*8+7] */
                uint8_t lo = (uint8_t)(g3 * 8);
                uint8_t hi = (uint8_t)(g3 * 8 + 7);
                if (mid < lo) mid = lo;
                if (mid > hi) mid = hi;
                new_g2 = (uint8_t)(mid & 0x07);
            } else {
                /* 行首/行尾: 退化成用 G3 的区间中点 (不引入新噪声) */
                new_g2 = 4;
            }
            px[1] = (uint8_t)((px[1] & 0x1F) | (new_g2 << 5));
        }
    }
}

/* ===========================================================================
 * 【软件亮度稳定器】—— 解决 GC0308 自动曝光的周期性震荡 (画面闪烁)
 * ---------------------------------------------------------------------------
 * 【问题现象】
 *   画面亮度在 11000 ~ 27000 之间来回跳, 周期 8 秒, 三档 (0xd4 = 0x0B/0x18/0x1D)。
 *   用户看到的就是"忽明忽暗", 主观上就是"闪烁"。
 *
 * 【取证链 (全部实测, 不是猜测)】
 *   1. 全寄存器扫描 (page0/1/2/3, 0x00-0xff) 对比多帧快照:
 *      唯一在变的是 0xd4 -> 0x18/0x1D/0x0B, 周期 8 秒。
 *   2. 0xd4 与画面亮度严格对应 (Pearson = 0.89):
 *      0xd4=0x0B -> 平均亮度 14349;  0xd4=0x1D -> 平均亮度 28885。
 *   3. 传感器【内置彩条】亮度稳定 (幅度 4%), 只有真实场景在跳
 *      -> 确认是 AE 根据场景画面来回调, 不是数据链路坏。
 *   4. 尝试关掉 AEC/AGC (0x22 各种取值, 含逐 bit 清零)、改 AEC 目标 (0xd3)、
 *      设 min/max 积分时间与增益上下限 (0x53-0x59)、改 PCLK 分频 ——
 *      【全部无效】。GC0308 的 AE 没有暴露可写的"关掉"开关,
 *      判决值只写在一字节的 0xd4 里, 而写 0xd4 会被传感器立刻改回去。
 *
 * 【结论】
 *   硬件层面关不掉这个震荡, 所以在【软件层】把它抵消掉。
 *
 * 【做法】
 *   每帧算一个亮度基准 (取样若干行的平均值), 和"参考亮度"比较,
 *   然后按比例把 RGB565 每个通道缩放到参考亮度附近。
 *
 *   关键点: 缩放的是【每个通道的位值】, 所以画面结构和颜色关系都保留,
 *   只是整体明暗被拉回一致 —— 这正好抵消 AE 的震荡。
 *
 * 【为什么这样安全】
 *   - 只在"亮度偏离参考值超过阈值"时才动手 (±8% 以内不处理), 避免把
 *     正常的场景亮度变化也抹掉。
 *   - 缩放系数被限制在 [0.6, 1.6], 防止极端情况把画面弄黑或烧白。
 *   - 参考亮度本身做慢速跟随 (每帧只朝目标移动 1/16), 这样环境光真的
 *     变了 (比如开灯/关灯) 时画面还是会缓慢适应, 只是不会一帧亮一帧暗。
 *   - 逐像素处理, 320x240 = 76800 像素, 用整数运算, 一次扫描即可。
 */
#define BRIGHT_STAB_ENABLE     1
#define BRIGHT_STAB_TARGET     26000u   /* 参考亮度 (0..65535) */
/*
 * 缩放上下限。
 *
 * 必须覆盖 AE 的完整调节范围: 实测 AE 把帧亮度在 11000 ~ 27000 之间拉,
 * 所以要把 11000 拉到 26000 需要 scale = 2.36, 而把 27000 拉下来只需要 0.96。
 * 下限设 0.85 / 上限设 2.60, 保证【任何一帧都能被拉到目标附近】。
 * (早先上限只有 1.80, 结果暗帧拉不上去, 亮度就在两个台阶之间跳。)
 */
#define BRIGHT_STAB_MIN_SCALE  0.85f   /* 缩放下限 */
#define BRIGHT_STAB_MAX_SCALE  2.60f   /* 缩放上限 */
/*
 * 参考亮度的跟随速度。
 *
 * 必须【比 AE 的震荡周期慢得多】: AE 周期是 8 秒, 这里的时间常数约 30 秒,
 * 所以 8 秒周期内的明暗起伏会被平均掉, 参考亮度保持稳定。
 * 同时, 环境光真的变了 (开灯/关灯, 几分钟尺度), 参考亮度还是会慢慢跟上。
 */
#define BRIGHT_STAB_ADAPT      0.0042f  /* 1/240 -> 8fps 下约 30 秒时间常数 */

static volatile bool s_bright_stab = (BRIGHT_STAB_ENABLE != 0);
static volatile float s_bright_ref = (float)BRIGHT_STAB_TARGET;
static volatile uint32_t s_bright_adjust_count = 0;   /* 调整过的帧数 */
static volatile uint32_t s_bright_last_scale_pct = 100; /* 最近一次的缩放百分比 */

/*
 * 估算一帧的平均亮度 (0..65535)。
 *
 * 【量纲必须和 BRIGHT_STAB_TARGET 一致】
 * 这里直接取 RGB565 16bit 像素值的平均, 也就是"把整帧当成 16bit 灰度"的
 * 平均值。这样 BRIGHT_STAB_TARGET=26000 就对应"整帧平均 26000/65535 = 40%"
 * 的画面, 和在电脑上分析 rawdump 的结果可以直接对照。
 *
 * (早先版本用的是 (r*77+g*151+b*28)*32, 量纲比这个大 32 倍,
 *  导致缩放系数一直被夹在下限, 画面反而越调越暗。)
 *
 * 为了省时间, 只采样一部分行 (每隔 step 行采一行), 但每行全采 ——
 * 320 个像素足够稳定, 而且行间亮度分布的变化也能被采到。
 */
static uint32_t estimate_frame_luma(const uint8_t *buf, int width, int height)
{
    uint64_t sum = 0;
    uint32_t count = 0;
    int step = (height > 32) ? 4 : 1;

    for (int y = 0; y < height; y += step) {
        const uint8_t *row = buf + (size_t)y * width * 2;
        for (int x = 0; x < width; x++) {
            sum += (uint32_t)((row[x * 2] << 8) | row[x * 2 + 1]);
            count++;
        }
    }
    if (count == 0) {
        return 0;
    }
    return (uint32_t)(sum / count);
}

/*
 * 把整帧的亮度缩放到目标值。
 *
 * 做法: 逐像素取出 RGB565 的三个分量, 分别乘以缩放系数后夹回各自的最大值,
 * 再重新打包。整数运算, 用定点 (Q8) 表示系数。
 */
static void apply_brightness_scale(uint8_t *buf, int width, int height,
                                   float scale)
{
    if (scale <= 0.0f) {
        return;
    }

    /* Q8 定点: scale_q8 = scale * 256 */
    int32_t sq = (int32_t)(scale * 256.0f);
    if (sq < 1) {
        sq = 1;
    }

    for (int y = 0; y < height; y++) {
        uint8_t *row = buf + (size_t)y * width * 2;
        for (int x = 0; x < width; x++) {
            uint16_t p = (uint16_t)((row[x * 2] << 8) | row[x * 2 + 1]);
            uint32_t r = (p >> 11) & 0x1F;
            uint32_t g = (p >> 5) & 0x3F;
            uint32_t b = p & 0x1F;

            r = (uint32_t)(((int32_t)r * sq) >> 8);
            g = (uint32_t)(((int32_t)g * sq) >> 8);
            b = (uint32_t)(((int32_t)b * sq) >> 8);

            if (r > 31) r = 31;
            if (g > 63) g = 63;
            if (b > 31) b = 31;

            uint16_t out = (uint16_t)((r << 11) | (g << 5) | b);
            row[x * 2] = (uint8_t)(out >> 8);
            row[x * 2 + 1] = (uint8_t)(out & 0xFF);
        }
    }
}

/*
 * 对一帧做亮度稳定处理。
 *
 * 返回是否做了调整。
 *
 * ---------------------------------------------------------------------------
 * 【设计: 每帧都归一化到固定的参考亮度】
 *
 * 之前的两个版本都失败, 原因分别是:
 *   a) 带死区 + 参考亮度跟着当前帧走 -> 参考亮度被 AE 带着一起震荡;
 *   b) 亮度估计的量纲和目标值不一致 (差了 32 倍) -> 缩放系数一直被夹住,
 *      画面反而越调越暗。
 *
 * 现在的做法很直接:
 *   scale = TARGET / luma, 每一帧都这么算, 输出亮度恒等于 TARGET。
 *
 * 这样 AE 的 8 秒周期震荡被【完全抵消】: AE 把画面调暗, 我们就调亮回来。
 *
 * 【关于"环境光变化时慢慢适应"】
 * 本来想让参考亮度缓慢跟随环境, 但那等于给 AE 的震荡开了个口子。
 * 对"远程看护"这个场景来说, 画面亮度恒定比亮度自适应更重要 ——
 * 老人看到的一直是同一个亮度, 不会忽然刺眼或忽然看不清。
 * 所以参考亮度固定, 用 bref 命令可以随时改。
 */
static bool stabilize_brightness(uint8_t *buf, int width, int height)
{
    if (!s_bright_stab) {
        return false;
    }

    uint32_t luma = estimate_frame_luma(buf, width, height);
    if (luma == 0) {
        return false;
    }

    float ref = s_bright_ref;
    if (ref < 1000.0f) {
        ref = (float)BRIGHT_STAB_TARGET;
    }

    /* 缩放系数: 把当前亮度拉到参考亮度 */
    float scale = ref / (float)luma;
    if (scale < BRIGHT_STAB_MIN_SCALE) scale = BRIGHT_STAB_MIN_SCALE;
    if (scale > BRIGHT_STAB_MAX_SCALE) scale = BRIGHT_STAB_MAX_SCALE;

    apply_brightness_scale(buf, width, height, scale);

    s_bright_adjust_count++;
    s_bright_last_scale_pct = (uint32_t)(scale * 100.0f);
    return true;
}

/* 请求抓一帧原始 RGB565 (由 /raw 端点设置) */
static volatile bool s_raw_capture_requested = false;

/* 请求通过串口导出一帧原始 RGB565 (由 "rawdump" 命令设置) */
static volatile bool s_raw_dump_requested = false;

static volatile uint32_t s_noise_rows = 0;        /* 最近一帧的噪点行数 */
static volatile uint32_t s_noise_rows_total = 0;  /* 累计噪点行数 */
static volatile uint32_t s_noise_frames = 0;      /* 参与统计的帧数 */
static volatile uint32_t s_noise_jumps = 0;       /* 最近一帧的大跳变数 */
static volatile uint32_t s_noise_energy = 0;      /* 最近一帧的能量 */

/* 统计一帧 RGB565 的噪声 (采样若干行) */
static void measure_noise(const uint8_t *buf, int width, int height,
                          uint32_t *out_jumps, uint32_t *out_energy,
                          uint32_t *out_noisy_rows)
{
    uint32_t jumps = 0;
    uint32_t energy = 0;
    uint32_t noisy = 0;
    uint32_t pairs = 0;

    int step = height / NOISE_PROBE_ROWS;
    if (step < 1) {
        step = 1;
    }

    for (int y = 0; y < height; y += step) {
        const uint8_t *row = buf + (size_t)y * width * 2;
        uint32_t row_jumps = 0;
        uint32_t row_sum = 0;

        uint16_t prev = (uint16_t)((row[0] << 8) | row[1]);
        for (int x = 1; x < width; x++) {
            uint16_t cur = (uint16_t)((row[x * 2] << 8) | row[x * 2 + 1]);
            int diff = (int)cur - (int)prev;
            if (diff < 0) {
                diff = -diff;
            }
            row_sum += (uint32_t)diff;
            if (diff > NOISE_JUMP_THRESH) {
                row_jumps++;
            }
            prev = cur;
        }

        jumps += row_jumps;
        energy += row_sum / (uint32_t)(width - 1);
        pairs++;
        if (row_jumps >= 8) {
            noisy++;
        }
    }

    if (out_jumps)      *out_jumps = jumps;
    if (out_energy)     *out_energy = pairs ? (energy / pairs) : 0;
    if (out_noisy_rows) *out_noisy_rows = noisy;
}

/*
 * LCD 显示路径开关 (A/B 对照用)
 *
 * 【默认值改为 true = 直绘】—— 理由是实测, 不是猜:
 *
 *   旧路径 (false): 每 200ms 做一次 153KB 的 PSRAM -> PSRAM memcpy。
 *   实测耗时 4.9ms, 是一口气连续读完写完的。而就在这 4.9ms 里,
 *   摄像头的 DVP DMA 正在往 PSRAM 里写【下一帧】。PSRAM 总线是共享的,
 *   我们这一口气把总线占满, DVP DMA 抢不到带宽, 下一帧就丢数据。
 *   丢掉的那些行在帧缓冲里是【上一次残留或垃圾】, 画到屏幕上就是一条条花带,
 *   这正是"闪花屏"的直接来源。
 *
 *   直绘 (true): 从摄像头帧缓冲分批画到屏幕, 零 PSRAM->PSRAM 大块拷贝。
 *   每批画完让出 CPU 给 DVP DMA (lcd_draw_rgb565 里的 vTaskDelay)。
 *   帧缓冲全程握在手里, 摄像头不会覆盖它 -> 也不会撕裂。
 *
 * 两种路径都【不影响】花屏的真正根因 (DVP 写 PSRAM 丢 bit, 见上面的
 * s_psram_dma_mode 说明), 但直绘能额外去掉一个明显的干扰项,
 * 让 A/B 对照更干净。
 */
static volatile bool s_lcd_direct_draw = true;

/*
 * 【LCD 显示模式】(运行时可切换, 用串口命令改)
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
 *
 * 【必须声明在 camera_producer_task 之前】
 *   直绘发生在生产者任务里, 而 lcd1/lcd2 模式发生在 lcd_preview_task 里。
 *   两者会同时往屏幕画, 所以生产者必须知道自己是不是该让路。
 */
typedef enum {
    LCD_MODE_CAMERA = 0,
    LCD_MODE_PATTERN = 1,
    LCD_MODE_SYNTH = 2,
} lcd_mode_t;

static volatile lcd_mode_t s_lcd_mode = LCD_MODE_CAMERA;

/*
 * ---------------- 屏幕提示的显示所有权 (需求文档 §3.8) ----------------
 *
 * 背景:
 *   屏幕提示 (lcd_prompt.c) 要在摄像头画面【上面】叠一条横幅。
 *   如果摄像头直绘继续跑, 下一帧就把横幅盖掉了, 提示等于没显示。
 *
 * 做法:
 *   提示任务画横幅前调 lcd_view_pause() -> 生产者暂停往屏幕直绘。
 *   提示到期后调 lcd_view_resume() -> 生产者恢复直绘,
 *   下一帧画面自然把横幅区域覆盖回来 (不用我们重画背景)。
 *
 * 为什么不用互斥锁:
 *   生产者和提示任务都要往屏幕写, 如果用锁, 提示要等当前那一帧画完
 *   (最长 40ms) 才能拿到锁 —— 能接受, 但更麻烦的是"暂停"期间
 *   生产者要不要一直持锁? 持着的话提示就永远拿不到。
 *   所以这里用一个【标志位 + 等待】的组合:
 *     - 标志位是 volatile bool, 生产者每帧检查
 *     - 提示任务设置标志后等一小会儿, 让生产者完成当前帧
 *
 * 【关键】提示期间画面是【静止的旧画面 + 横幅】, 不是撕裂。
 *   因为生产者是整帧画完的, 暂停只发生在帧边界。
 */
static volatile bool     s_lcd_view_paused = false;
static volatile uint32_t s_lcd_pause_count = 0;   /* 暂停次数 (调试用) */

/* ---------------- 噪点行统计开关 (排障用) ---------------- */
static volatile bool s_noise_enabled = false;

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

        /*
         * 一连上网就立刻对时。
         *
         * 设备没有 RTC, 开机后的系统时间是编译时刻。
         * 活动检测的 last_active_ts、告警时间戳、夜间规则全都依赖真实时间,
         * 所以这里必须第一时间校时 (需求文档 §3.9)。
         *
         * 对时失败不影响主流程 —— time_sync 任务会自己重试。
         */
        time_sync_start();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_AP_START) {
        ESP_LOGI(TAG, "热点已启动，SSID: %s", WIFI_SSID);
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

/*
 * 扫描用的 Wi-Fi 初始化: 只把射频打开, 不连任何网络。
 *
 * 和 wifi_init() 的区别:
 *   wifi_init() 会 esp_wifi_set_config + esp_wifi_start, 直接开始连
 *   —— 之前连不上 5GHz 的 WMU-EDU 就会疯狂重连, 打爆 GDMA 导致花屏。
 *   这里只 start 不 connect, 射频能扫, 但不会去骚扰任何 AP。
 */
static void wifi_scan_init(void)
{
    if (s_wifi_events == NULL) {
        s_wifi_events = xEventGroupCreate();
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    s_netif = esp_netif_create_default_wifi_sta();
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Wi-Fi 射频已启动 (仅扫描, 不连接)");
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

    /*
     * 【2026-11-12】SSID / 密码优先从 NVS 读, 读不到才用编译期默认值。
     *
     * 这样换网络 (换了校园网 / 路由器改名 / 换密码) 只要一条串口命令,
     * 不用改源码重新烧录。见 wifi_config.c。
     */
    const char *ssid = wifi_config_ssid();
    const char *pass = wifi_config_pass();

    /*
     * 认证方式按密码是否为空自动选。
     * 之前固定 WPA_WPA2_PSK, 对 OPEN 网络 (2.4GHz 的 WMU-EDU) 会失败。
     * 校园网经常是开放网络或 WPA2-Enterprise, 这里按密码有无自动切。
     */
    wifi_auth_mode_t authmode;
    if (pass[0] == '\0') {
        authmode = WIFI_AUTH_OPEN;
    } else {
        authmode = WIFI_AUTH_WPA_WPA2_PSK;
    }

    wifi_config_t sta_cfg = {0};
    /* ssid/pass 是 const char*, 但 esp_wifi_set_config 要的是定长数组,
     * 所以这里必须拷贝一份 (顺便截断到协议长度上限)。 */
    strncpy((char *)sta_cfg.sta.ssid, ssid, sizeof(sta_cfg.sta.ssid) - 1);
    strncpy((char *)sta_cfg.sta.password, pass, sizeof(sta_cfg.sta.password) - 1);
    sta_cfg.sta.threshold.authmode = authmode;
    /* 扫描时也用同一个认证方式, 否则扫不到企业网络的隐藏 SSID */
    sta_cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    /*
     * 顺序很重要: 必须先 esp_wifi_set_mode() 再 esp_wifi_set_config()。
     *
     * esp_wifi_init() 之后模式是 WIFI_MODE_NULL, 此时直接
     * esp_wifi_set_config(WIFI_IF_STA, ...) 会返回
     * ESP_ERR_WIFI_MODE (0x3005), 下面这行 ESP_ERROR_CHECK 会直接
     * abort() -> 板子无限重启 -> 每次重启播一声开机自检音
     * (听起来就是"一直嘀嘀嘀")。
     *
     * 【2026-11-08 实测踩坑】原来这里两行是反的:
     *     set_config(STA) -> set_mode(STA)   // 必崩
     * 正确的是:
     *     set_mode(STA)   -> set_config(STA) -> start()
     *
     * AP 分支早就写对了 (见上面那段注释), 只有 STA 分支漏改。
     */
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    /*
     * 【2026-11-12】Wi-Fi 内存占用的说明。
     *
     * 内部 RAM 只剩 300 多字节时, 任务 TCB 全部分配失败
     * ("任务 button_task 的 TCB 分配失败 (内部RAM)")。
     *
     * 内部 RAM 的大头已经定位到 httpd (已改成栈放 PSRAM, 见
     * http_server_start 里的注释)。Wi-Fi 驱动这块, IDF 5.3 的
     * wifi_sta_config_t 还没有 ampdu_rx_enable / ampdu_tx_enable
     * 字段 (那是 5.4 才加的), 没法从配置层面关。
     *
     * 能做的是别让 Wi-Fi 疯狂重连 —— 每次 esp_wifi_connect() 都会
     * 在内部 RAM 上分配扫描缓冲。退避逻辑在 wifi_event_handler 里。
     */

    ESP_LOGI(TAG, "Wi-Fi STA 模式启动中 (SSID=%s, 认证=%s, 来源=%s)",
             ssid,
             authmode == WIFI_AUTH_OPEN ? "OPEN" : "WPA/WPA2",
             wifi_config_from_nvs() ? "NVS" : "编译期默认值");
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

/* ---------------- I2C 总线扫描 (找 codec) ----------------
 *
 * 目的: 板载音频到底是 ES8311+ES7210 还是 ES8388, 文档说法不一,
 * 必须靠实测确认 —— 引脚配错会直接和摄像头数据线撞车导致花屏。
 *
 * 已知设备 (实测):
 *   0x18 ES8311 codec, 0x40 ES7210 麦克风 ADC, 0x19 待确认
 *   0x21 GC0308 摄像头 (SCCB), 0x59 AW9523B IO 扩展器
 *
 * 默认关闭 (AUDIO_I2C_SCAN_ENABLE=0), 需要排查硬件接线时才打开。
 */
#define AUDIO_I2C_SCAN_ENABLE  0

static void i2c_scan_audio(void)
{
    ESP_LOGI(TAG, "==== I2C 总线扫描 (找音频 codec) ====");
    uint8_t reg = 0x00;
    uint8_t val = 0;
    for (int addr = 0x08; addr <= 0x77; addr++) {
        /*
         * 用"读寄存器"方式探测: 先写 1 字节寄存器号, 再读 1 字节。
         * 0 长度写不可靠 (AW9523B 明明在线却不应答)。
         */
        esp_err_t err = i2c_master_write_read_device(BOARD_I2C_PORT, addr,
                                                     &reg, 1, &val, 1,
                                                     pdMS_TO_TICKS(20));
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "  I2C 0x%02X 应答 (reg0=%02X)", addr, val);
        } else if (err != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "  I2C 0x%02X 错误: %s", addr, esp_err_to_name(err));
        }
    }
    ESP_LOGI(TAG, "==== 扫描结束 ====");
}

/*
 * ---------------------------------------------------------------------------
 * 【2026-11-05 修复"屏幕无画面"】I2C 总线恢复 (bus recovery)
 * ---------------------------------------------------------------------------
 *
 * 症状:
 *   开机后 AW9523B / SCCB / codec 全部 ESP_ERR_TIMEOUT,
 *   重试 10 次 (3.9s) 也没用, 但运行到 20 秒后手动读就能通。
 *
 * 根因: I2C 总线被某个器件"钳死"了。
 *   I2C0 (SDA=3/SCL=2) 上挂了 4 个器件:
 *     AW9523B(0x59) / GC0308(0x21) / ES8311(0x18) / ES7210(0x40)
 *   其中有器件在【还没被正确复位】时会一直把 SDA 或 SCL 按住不放
 *   (典型是 NXP 系列的 "SDA 拉低等待 ACK" 状态)。
 *   一旦有一个器件按住 SDA, 主机发什么都收不到 ACK —— 全部超时。
 *
 *   为什么 20 秒后会自己好:
 *     ES8311/ES7210 的初始化过程里会做软件复位, 顺带把总线释放了。
 *     所以真正起作用的不是"等", 而是 codec 初始化时对总线的干预。
 *
 * 标准的解法是【总线恢复序列】(I2C 规范 3.1.16):
 *   1. 把 SDA/SCL 配成【开漏输入】
 *   2. 手动拉高 SCL, 然后检查 SDA 是否已经释放
 *   3. 如果 SDA 还是低 -> 主控模拟主机, 手动打 9 个 SCL 脉冲
 *      (每个器件最多会释放一个 ACK 位), 让挂住的器件把 SDA 放掉
 *   4. 再补一个 STOP 条件 (SDA 从低到高, 此时 SCL 为高)
 *   5. 恢复 SDA/SCL 为正常的开漏 + 上拉, 交给驱动
 *
 * 这就是"救砖"标准做法, 对所有"总线被钳死"的场景都有效。
 * ---------------------------------------------------------------------------
 */

/*
 * ---------------------------------------------------------------------------
 * 【2026-11-05】I2C 全地址扫描 —— 一次性看清总线上到底谁在应答
 * ---------------------------------------------------------------------------
 *
 * 背景:
 *   开机时 AW9523B / SCCB 全部超时, 但运行到 ~20 秒后又能读到。
 *   光看"超时"不知道是"总线被钳死"、"地址错了"还是"器件没上电"。
 *
 *   全地址扫描能一次给出全部答案:
 *     - 有地址应答         -> 总线是好的, 问题在具体器件或地址
 *     - 一个都不应答且
 *       SDA/SCL 都是 0     -> 总线被钳死 (需要 bus recovery)
 *     - 一个都不应答但
 *       SDA/SCL 都是 1     -> 总线空闲, 但所有器件都没上电
 */
static void i2c_scan_once(void)
{
    int sda = gpio_get_level(BOARD_I2C_SDA_PIN);
    int scl = gpio_get_level(BOARD_I2C_SCL_PIN);

    ESP_LOGI(TAG, "==== I2C 总线扫描 ====");
    ESP_LOGI(TAG, "  初始电平: SDA=%d SCL=%d", sda, scl);

    uint8_t reg = 0x00;
    uint8_t val = 0;
    int found = 0;
    for (int addr = 0x08; addr <= 0x77; addr++) {
        esp_err_t err = i2c_master_write_read_device(BOARD_I2C_PORT, addr,
                                                     &reg, 1, &val, 1,
                                                     pdMS_TO_TICKS(20));
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "  0x%02X 应答 (reg0x00=0x%02X)", addr, val);
            found++;
        }
    }
    ESP_LOGI(TAG, "  共 %d 个地址应答", found);
    ESP_LOGI(TAG, "==== 扫描结束 ====");
}

/*
 * 总线监视任务: 每 2 秒做一次全地址扫描, 连续记录 30 秒。
 * 用来确定"总线到底什么时候恢复"以及"恢复时哪些器件一起上线"。
 */
static void i2c_monitor_task(void *arg)
{
    for (int round = 1; round <= 15; round++) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        i2c_scan_once();
    }
    vTaskDelete(NULL);
}

/*
 * ---------------------------------------------------------------------------
 * 【2026-11-05 修复"屏幕无画面"】I2C 总线恢复 (bus recovery)
 * ---------------------------------------------------------------------------
 *
 * 症状:
 *   开机后 AW9523B / SCCB / codec 全部 ESP_ERR_TIMEOUT,
 *   但运行到 20 秒后手动读就能通 (0x18/0x19/0x40/0x59 四个地址都应答)。
 *
 * 【一个必须避开的诊断陷阱】
 *   gpio_get_level() 只有在引脚是【纯输入】时才读引脚真实电平;
 *   一旦配成 GPIO_MODE_INPUT_OUTPUT_OD (输出使能), 它读的是
 *   GPIO.out_reg —— 也就是"我们/外设请求输出的值", 不是引脚上的电平。
 *
 *   I2C 外设接管引脚后, out_reg 会被外设的状态机写入。
 *   如果外设卡在一个未完成的事务里 (对端不应答, 主机一直等 ACK),
 *   out_reg 就会停在 SDA=0/SCL=0 —— 这正是我们最初看到的现象。
 *   所以"读出来是 0"根本不能证明引脚被物理拉低, 之前的判断是错的。
 *
 *   正确测法: 先把引脚切成 GPIO_MODE_INPUT (纯输入, 输出关闭),
 *   此时 gpio_get_level() 走 in_reg, 读到的才是真实电平。
 *
 * 恢复序列 (I2C 规范 3.1.16 的 bus recovery):
 *   1. 卸载 I2C 驱动 —— 顺带复位外设状态机 (卡在等 ACK 的事务会解开)
 *   2. 用纯输入模式读一次真实电平, 判断到底是被拉低还是外设假象
 *   3. 切到开漏模式, 手动打大量 SCL 脉冲
 *      (每个挂在总线上的器件最多释放一个 ACK 位)
 *   4. 补一个 STOP 条件 (SCL 为高时 SDA 从低到高)
 *   5. 再用纯输入模式确认电平是否已释放
 *   6. 把 I2C 驱动装回去
 * ---------------------------------------------------------------------------
 */

/*
 * 把 SDA/SCL 临时切成纯输入, 读取引脚【真实】电平。
 * 注意: 调用前必须先卸载 I2C 驱动, 否则外设还在驱动引脚。
 */
static void board_i2c_real_levels(int *sda_out, int *scl_out)
{
    const int scl_pin = BOARD_I2C_SCL_PIN;
    const int sda_pin = BOARD_I2C_SDA_PIN;

    gpio_config_t in_conf = {
        .pin_bit_mask = (1ULL << scl_pin) | (1ULL << sda_pin),
        .mode         = GPIO_MODE_INPUT,          /* 纯输入 -> 读 in_reg = 真实电平 */
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&in_conf));
    vTaskDelay(pdMS_TO_TICKS(5));

    if (sda_out) *sda_out = gpio_get_level(sda_pin);
    if (scl_out) *scl_out = gpio_get_level(scl_pin);

    /*
     * 【必须还原】刚才把引脚切成了纯输入, 这等于把引脚从 I2C 外设上摘下来。
     * 如果不还原, 后续的 I2C 事务根本发不出去 (引脚不再连到外设)。
     *
     * i2c_param_config() 内部会调 i2c_set_pin(), 把引脚重新接回 I2C 外设。
     */
    i2c_config_t restore = {
        .mode                = I2C_MODE_MASTER,
        .sda_io_num         = BOARD_I2C_SDA_PIN,
        .scl_io_num         = BOARD_I2C_SCL_PIN,
        .sda_pullup_en      = GPIO_PULLUP_ENABLE,
        .scl_pullup_en      = GPIO_PULLUP_ENABLE,
        .master.clk_speed    = BOARD_I2C_FREQ_HZ,
    };
    i2c_param_config(BOARD_I2C_PORT, &restore);
    vTaskDelay(pdMS_TO_TICKS(5));
}

/*
 * ---------------------------------------------------------------------------
 * 【2026-11-05】I2C 引脚物理状态探针 (开机最早期)
 * ---------------------------------------------------------------------------
 *
 * 目的: 在任何 I2C 驱动/外设碰过这两个引脚之前, 搞清楚 SDA/SCL
 *       在【物理层面】到底是什么状态。
 *
 * 用 4 种 GPIO 配置分别读, 交叉验证:
 *   (a) INPUT + 内部上拉   -> 有外部上拉时读 1; 无外部上拉时靠内部上拉也能读 1
 *   (b) INPUT 无上拉       -> 只有外部上拉才能读 1; 否则读 0 (悬空/被拉低)
 *   (c) 推挽输出 1 后读回   -> 能读 1 说明线没被硬短
 *   (d) 推挽输出 0 后读回   -> 能读 0 说明 MCU 真的能驱动这条线
 *
 * (a)(b) 的组合能区分"没有外部上拉"和"被拉低":
 *   (a)=1 (b)=0  -> 只有内部上拉, 外部上拉缺失/太弱
 *   (a)=0 (b)=0  -> 线被拉低 (有器件在钳死)
 */
static void i2c_pin_physical_probe(void)
{
    const int scl_pin = BOARD_I2C_SCL_PIN;
    const int sda_pin = BOARD_I2C_SDA_PIN;

    ESP_LOGW(TAG, "==== I2C 引脚物理探针 (SDA=%d SCL=%d) ====",
             BOARD_I2C_SDA_PIN, BOARD_I2C_SCL_PIN);

    /* (a) 纯输入 + 内部上拉 */
    gpio_config_t c = {
        .pin_bit_mask = (1ULL << scl_pin) | (1ULL << sda_pin),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&c);
    vTaskDelay(pdMS_TO_TICKS(20));
    int a_sda = gpio_get_level(sda_pin), a_scl = gpio_get_level(scl_pin);

    /* (b) 纯输入, 无上拉 */
    c.pull_up_en = GPIO_PULLUP_DISABLE;
    gpio_config(&c);
    vTaskDelay(pdMS_TO_TICKS(20));
    int b_sda = gpio_get_level(sda_pin), b_scl = gpio_get_level(scl_pin);

    /* (c) 推挽输出 1, 读回 */
    c.mode = GPIO_MODE_OUTPUT;
    c.pull_up_en = GPIO_PULLUP_DISABLE;
    gpio_config(&c);
    gpio_set_level(sda_pin, 1);
    gpio_set_level(scl_pin, 1);
    vTaskDelay(pdMS_TO_TICKS(20));
    int c_sda = gpio_get_level(sda_pin), c_scl = gpio_get_level(scl_pin);

    /* (d) 推挽输出 0, 读回 */
    gpio_set_level(sda_pin, 0);
    gpio_set_level(scl_pin, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    int d_sda = gpio_get_level(sda_pin), d_scl = gpio_get_level(scl_pin);

    ESP_LOGW(TAG, "  (a) 输入+内部上拉 : SDA=%d SCL=%d", a_sda, a_scl);
    ESP_LOGW(TAG, "  (b) 输入无上拉   : SDA=%d SCL=%d", b_sda, b_scl);
    ESP_LOGW(TAG, "  (c) 推挽输出1读回: SDA=%d SCL=%d", c_sda, c_scl);
    ESP_LOGW(TAG, "  (d) 推挽输出0读回: SDA=%d SCL=%d", d_sda, d_scl);

    if (a_sda == 0 && b_sda == 0 && c_sda == 1) {
        ESP_LOGE(TAG, "  => SDA 只能靠 MCU 推挽驱动才有高电平: 外部上拉缺失/失效");
    } else if (a_sda == 0 && c_sda == 0) {
        ESP_LOGE(TAG, "  => SDA 被硬短到地: 有器件/电路把它钳死了");
    }
    if (a_scl == 0 && b_scl == 0 && c_scl == 1) {
        ESP_LOGE(TAG, "  => SCL 只能靠 MCU 推挽驱动才有高电平: 外部上拉缺失/失效");
    } else if (a_scl == 0 && c_scl == 0) {
        ESP_LOGE(TAG, "  => SCL 被硬短到地: 有器件/电路把它钳死了");
    }

    /* 还原成输入, 交给后面的 I2C 初始化 */
    c.mode = GPIO_MODE_INPUT;
    c.pull_up_en = GPIO_PULLUP_ENABLE;
    gpio_config(&c);
}

/*
 * ---------------------------------------------------------------------------
 * 【2026-11-05】I2C 多速率探测 —— 验证"上拉太弱"假说
 * ---------------------------------------------------------------------------
 *
 * 假说: 板子上 SDA/SCL 没有像样的外部上拉电阻, 只靠 ESP32 内部上拉
 *      (~45kΩ) 撑着。内部上拉在 100kHz 下 RC 上升沿太缓,
 *      AW9523B 的 SCL 高电平建立时间要求达不到 -> 一直不应答。
 *      而后面大量 SCCB 时钟脉冲 / codec 初始化改变了时序, 就通了。
 *
 * 验证方法: 依次用几个速度去读 AW9523B 的 ID 寄存器 (0x10),
 *      哪个速度能读出来, 就说明是时序/上升沿问题。
 *
 * 只读 ID, 不写任何配置, 风险为零。
 */
static void i2c_speed_probe(void)
{
    static const uint32_t speeds[] = {100000, 50000, 20000, 10000, 5000};
    uint8_t id = 0;

    ESP_LOGW(TAG, "==== I2C 多速率探测 (目标 AW9523B 0x59, 寄存器 0x10) ====");

    for (size_t i = 0; i < sizeof(speeds) / sizeof(speeds[0]); i++) {
        i2c_config_t conf = {
            .mode                = I2C_MODE_MASTER,
            .sda_io_num         = BOARD_I2C_SDA_PIN,
            .scl_io_num         = BOARD_I2C_SCL_PIN,
            .sda_pullup_en      = GPIO_PULLUP_ENABLE,
            .scl_pullup_en      = GPIO_PULLUP_ENABLE,
            .master.clk_speed    = speeds[i],
        };
        esp_err_t err = i2c_param_config(BOARD_I2C_PORT, &conf);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "  %6lu Hz: param_config 失败 %s", (unsigned long)speeds[i], esp_err_to_name(err));
            continue;
        }
        vTaskDelay(pdMS_TO_TICKS(50));

        id = 0;
        err = i2c_master_write_read_device(BOARD_I2C_PORT, AW9523B_I2C_ADDR,
                                           (uint8_t[]){AW9523B_REG_ID}, 1, &id, 1,
                                           pdMS_TO_TICKS(200));
        ESP_LOGW(TAG, "  %6lu Hz: %s (id=0x%02x)", (unsigned long)speeds[i],
                 (err == ESP_OK) ? "应答" : esp_err_to_name(err), id);
    }

    /* 探测完把速度恢复成默认值 */
    i2c_config_t conf = {
        .mode                = I2C_MODE_MASTER,
        .sda_io_num         = BOARD_I2C_SDA_PIN,
        .scl_io_num         = BOARD_I2C_SCL_PIN,
        .sda_pullup_en      = GPIO_PULLUP_ENABLE,
        .scl_pullup_en      = GPIO_PULLUP_ENABLE,
        .master.clk_speed    = BOARD_I2C_FREQ_HZ,
    };
    i2c_param_config(BOARD_I2C_PORT, &conf);
}

/*
 * 执行一次 I2C 总线恢复序列。
 * 返回 true = 恢复后总线干净 (SDA/SCL 都是高), false = 仍被拉低。
 */
static bool board_i2c_bus_recover(void)
{
    /*
     * 【为什么必须用 HAL 层, 不能用 GPIO 手动打脉冲】
     *
     * ESP32-S3 的 I2C 引脚通过 GPIO matrix 接到外设上。只要外设还在工作,
     * 我们用 gpio_set_level() 写的值根本出不去 —— 实测打了 9 个和 200 个
     * 脉冲, SDA/SCL 电平纹丝不动。
     *
     * 外设本身提供了原生的解法:
     *   i2c_ll_master_fsm_rst()  复位卡在等 ACK 的主控状态机
     *   i2c_ll_master_clr_bus()  让硬件自动发送时钟脉冲, 直到从机释放 SDA
     *
     * 【重要】绝对不能在这里 i2c_driver_delete()。
     *   旧版驱动 (components/driver/i2c) 的对象是全局静态数组,
     *   delete 之后 install 会失败 ("i2c driver install error"),
     *   那样整条 I2C 就彻底废了 —— 之前踩过这个坑。
     *   我们只操作外设寄存器, 不碰驱动生命周期。
     */

    /* 1) 复位卡死的主控状态机 */
    i2c_ll_master_fsm_rst(I2C_LL_GET_HW(BOARD_I2C_PORT));
    vTaskDelay(pdMS_TO_TICKS(20));

    /* 2) 硬件清总线: 让硬件自动打脉冲, 直到从机释放 SDA */
    i2c_ll_master_clr_bus(I2C_LL_GET_HW(BOARD_I2C_PORT), 9, true);
    vTaskDelay(pdMS_TO_TICKS(200));

    /*
     * 3) 读真实电平。
     *
     * 【关键】读之前必须先把引脚从 I2C 外设上摘下来 (切成纯输入),
     * 否则 gpio_get_level() 读的是 out_reg (我们请求输出的值),
     * 不是引脚上的真实电平 —— 之前几次诊断就栽在这里。
     *
     * 读完再切回去 (board_i2c_real_levels 内部已经做了还原)。
     */
    int sda = 0, scl = 0;
    board_i2c_real_levels(&sda, &scl);
    ESP_LOGW(TAG, "I2C 真实电平[硬件清总线后]: SDA=%d SCL=%d%s",
             sda, scl,
             (sda == 0 || scl == 0) ? "  <== 仍被拉低" : "  (已释放)");

    return (sda != 0 && scl != 0);
}

/* 板载电源使能 + 背光 + 摄像头复位 (走 AW9523B) */
/*
 * ---------------------------------------------------------------------------
 * 【2026-11-05 修复"屏幕无画面"】AW9523B 开机瞬间不应答, 必须重试
 * ---------------------------------------------------------------------------
 *
 * 症状:
 *   开机日志:
 *     W aw9523b: AW9523B 无应答 (addr=0x59): ESP_ERR_TIMEOUT
 *     W kanhu:   AW9523B 不可用，跳过电源/背光/复位控制
 *     W kanhu:   SCCB: 写 0xFE 失败: ESP_ERR_TIMEOUT
 *     W kanhu:   SCCB: 读 PID 失败 (0x21): ESP_ERR_TIMEOUT
 *     E camera:  Camera probe failed with error 0x106(ESP_ERR_NOT_SUPPORTED)
 *   -> 屏幕永远黑, 浏览器也没有画面。
 *
 * 根因链:
 *   AW9523B 是板载 IO 扩展器, 它管着:
 *     XIO11 = 板载 3.3V 总电源使能
 *     XIO14 = 摄像头 2.8V 供电使能
 *     XIO15 = 摄像头复位
 *     XIO8  = LCD 背光 (低电平点亮)
 *   AW9523B 一旦不应答, camera_power_reset() 直接 return,
 *   于是【摄像头的 2.8V 供电从未使能、复位从未给出】——
 *   摄像头根本没电, SCCB 当然全超时, 画面当然出不来。
 *
 * 为什么开机瞬间 I2C 不通 (实测):
 *   开机后 ~1.2s 时 AW9523B 无应答, 但到 ~2s 就正常了。
 *   实测脚本 tools/i2c_probe.py: t=0.0s 无应答, t=2.0s 起 OK。
 *   板子上 I2C0 (SDA=3/SCL=2) 上挂了 4 个器件:
 *     AW9523B (0x59) / GC0308 (0x21) / ES8311 (0x18) / ES7210 (0x40)
 *   刚上电时这些器件的电源轨 (特别是通过 AW9523B 供电的 2.8V 轨)
 *   还没稳定, 会把总线拉死一段时间, 表现为所有地址都超时。
 *
 * 所以正确做法是【重试】而不是改引脚:
 *   在 AW9523B 上最多重试 N 次, 每次间隔 300ms, 总预算 ~3s。
 *   一旦应答成功, 立刻执行完整的电源/背光/复位时序。
 *
 * 【为什么不在这里直接 abort 重启】
 *   重启解决不了问题 —— 每次重启都是同样的"上电瞬间总线未稳",
 *   会变成无限重启循环。重试等待才是对症的。
 * ---------------------------------------------------------------------------
 */
#define AW9523B_RETRY_MAX      20      /* 最多重试 20 次 */
#define AW9523B_RETRY_GAP_MS   500     /* 每次间隔 500ms (总窗口 30s) */

static void camera_power_reset(void)
{
    bool aw_ok = false;

    for (int attempt = 0; attempt < AW9523B_RETRY_MAX; attempt++) {
        if (aw9523b_init(BOARD_I2C_PORT, AW9523B_I2C_ADDR) == ESP_OK) {
            aw_ok = true;
            break;
        }
        ESP_LOGW(TAG, "AW9523B 第 %d 次探测失败, %dms 后重试",
                 attempt + 1, AW9523B_RETRY_GAP_MS);

        /*
         * 做一次 I2C 总线恢复。
         *
         * 恢复会卸载/重装 I2C 驱动, 所以只能在 app_main 早期做
         * (此时还没有别的任务在用 I2C)。
         */
        board_i2c_bus_recover();

        vTaskDelay(pdMS_TO_TICKS(AW9523B_RETRY_GAP_MS));
    }

    if (!aw_ok) {
        ESP_LOGE(TAG, "AW9523B 重试 %d 次仍无应答 —— 摄像头供电/复位无法配置, 屏幕将无画面",
                 AW9523B_RETRY_MAX);
        ESP_LOGE(TAG, "  排查: 1)板子是否上电 2)I2C0 (SDA=3/SCL=2) 是否被拉死 3)AW9523B 是否虚焊");
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

    /*
     * 【电池电路方向】XIO2 输出 / XIO3 输入。
     *
     * 正点原子官方 config.h 里:
     *   XIO_BAT_CHRG_EN (XIO2) = 电池采样使能, 【输出】, 拉低导通分压
     *   XIO_BAT_CHRG    (XIO3) = 充电状态,   【输入】, 低 = 正在充电
     *
     * 为什么必须单独处理:
     *   官方 DRV_IO_EXP_OUTPUT_MASK=0xFFFC / INPUT_MASK=0x0003 只把 K1/K2
     *   当输入, XIO3 在官方那版是被当输出的 (那版固件没做电池检测)。
     *   但 XIO3 是充电芯片的检测脚, 驱动它会短接检测电路, 状态是假的。
     *   所以这里显式把 XIO3 改回输入。
     *
     * 注意: 这一步放在 aw9523b_set_p0_push_pull() 之前不影响效果 ——
     *       CONFIG 寄存器 (方向) 和 CTL 寄存器 (推挽/开漏) 是两个独立寄存器,
     *       推挽设置只影响【输出脚】的电平驱动方式, 不改变方向。
     */
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_BAT_CHRG_EN, true);
    aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_BAT_CHRG, false);

    /*
     * P0 端口的输出驱动模式设为【推挽】。
     *
     * AW9523B 的 P0 默认是开漏 (Open-Drain), 需要板子上有外部上拉电阻。
     * 正点原子 BOX3 的 P0 外部上拉只有部分脚有, K1/K2 所在的 P0_0/P0_1
     * 实测读回恒为低 (INPUT P0 bit1 = 0), 说明这两脚没有有效的外部上拉,
     * 结果是 button_task 永久卡在"等松开"循环 -> BOOT 物理按键事件永远
     * 不生成, M2 验收直接卡死。
     *
     * 设成推挽后, 输出脚不再依赖外部上拉。虽然 K1/K2 是输入脚,
     * 但把整个 P0 统一成推挽可以避免任何一脚在开漏状态下被拉死。
     */
    aw9523b_set_p0_push_pull(BOARD_I2C_PORT, AW9523B_I2C_ADDR, true);

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

    /*
     * 电池采样使能脚 XIO2 默认【拉高】(不导通分压)。
     * 只有真正要读电量时才拉低, 读完立刻改回输入。
     * 一直拉低 = 一直给电池挂分压电阻, 白耗电。
     */
    aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, XIO_BAT_CHRG_EN, true);

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
        ESP_LOGI(TAG, "  期望: P0 bit2(BAT_CHRG_EN)=0(输出) bit3(BAT_CHRG)=1(输入)");
    }
}

/* ---------------- 摄像头 ---------------- */

/*
 * GC0308 不支持硬件 JPEG，必须先拿到 RGB565 再软件编码。
 *
 * ==================== 关键: 为什么用 YUV422 + 硬件转换器 ====================
 *
 * 之前这里写的是 .pixel_format = PIXFORMAT_RGB565, 结果画面全是花屏噪点。
 * 根因是【GC0308 实际上并没有按 RGB565 输出】。
 *
 * 实测证据 (通过新增的 /raw 端点导出原始帧缓冲, 在电脑上逐字节分析):
 *
 *   原始帧缓冲 (153600 字节) 的字节平面统计:
 *     高字节 (偶数字节) 相邻差 = 55.6
 *     低字节 (奇数字节) 相邻差 =  7.7
 *     低字节比高字节平滑 9.7 倍!
 *
 *   RGB565 里高字节是 R5+G3、低字节是 G2+B5,
 *   两个字节承载的信息量相当, 相邻差应该【同数量级】。
 *   低字节几乎恒定 => 低字节装的不是像素低位, 而是【色度】。
 *
 *   按 YUYV(YUV422 交错) 解码验证:
 *     Y (亮度, 每像素都有)  相邻差 = 45.2  <- 有细节
 *     U (色度, 两像素共享)  相邻差 =  7.7  <- 平滑
 *     V (色度, 两像素共享)  相邻差 =  7.7  <- 平滑
 *   完全符合 YUV422 的特征: 色度是隔行共享的, 所以比亮度平滑得多。
 *
 *   把同一帧按两种方式解码并排看:
 *     当 RGB565 解 -> 红绿噪点 (就是用户看到的花屏)
 *     按 YUYV 解   -> 清晰正常的画面
 *
 * 为什么之前【浏览器里看着还行】?
 *   JPEG 编码器会把高频噪点平滑掉, 所以流看起来只是"有点脏",
 *   而 LCD 显示的是原始 RGB565, 噪点原形毕露, 看着就是"一直花屏在闪"。
 *
 * 解决方案:
 *   1) 让 GC0308 输出 YUV422 (它的原生/稳定格式)
 *   2) 交给 ESP32-S3 的【硬件 YUV->RGB 转换器】处理
 *      (CONFIG_CAMERA_CONVERTER_ENABLED=y)
 *      转换在硬件里完成, CPU 零开销 —— 比软件转 YUV->RGB 快得多。
 *
 * 注意:
 *   - pixel_format 必须写成 PIXFORMAT_YUV422 (这是【传感器输出】格式)
 *   - conv_mode  = YUV422_TO_RGB565 (这是【转换器输出】格式)
 *   - 这样 esp_camera_fb_get() 拿到的就是真正的 RGB565,
 *     后面 JPEG 编码和 LCD 显示都不用改。
 */
/*
 * 【转换器开关】用不用 S3 的硬件 YUV->RGB 转换器。
 *
 * 这是一个必须用数据回答的问题, 因为两种配置各有各的问题:
 *
 *   用转换器 (conv_mode = YUV422_TO_RGB565):
 *     传感器输出 YUV422 -> 硬件转成 RGB565。
 *     优点: CPU 零开销。
 *     风险: 实测出来的现象是 "lo 字节极脏 (能量 37)、hi 字节干净 (能量 3)",
 *           而转换器输出是 RGB565, 两个字节承载的信息量应该相当 ——
 *           这个巨大的不对称【不正常】, 怀疑转换器根本没真正生效,
 *           我们拿到的还是原始 YUV422, 只是被当成 RGB565 解释了。
 *
 *   不用转换器 (pixel_format = RGB565):
 *     传感器直接输出 RGB565 (GC0308 支持, 见 gc0308.c 的 set_pixformat)。
 *     优点: 路径最短, 没有转换环节, 排除转换器的嫌疑。
 *     风险: RGB565 是 2 字节/像素, DVP 带宽需求和 YUV422 一样, 无所谓。
 *
 * 所以做成运行时开关, 用固件的 bad_rows 指标客观地选。
 */
static volatile bool s_use_converter = true;

/*
 * 【摄像头重配置的安全锁】
 *
 * 之前 `conv 0` 命令会直接调用 esp_camera_deinit(), 而那时候
 * camera_producer_task 正在 esp_camera_fb_get() 里 —— 两者并发,
 * 立刻 RTC_SW_CPU_RST 复位, 板子重启, 转换器又变回默认的"开"。
 *
 * 正确做法:
 *   1) 生产者每轮循环检查 s_cam_pause, 看到就【把帧还回去并退出循环】
 *      (在 esp_camera_fb_get() 之前检查, 保证不会握着帧时被打断);
 *   2) 重配置时先置 s_cam_pause, 等生产者确认已退出 (s_cam_paused=1),
 *      再做 deinit/init;
 *   3) 完成后清 s_cam_pause, 生产者自动恢复。
 */
static volatile bool s_cam_pause = false;    /* 请求生产者暂停 */
static volatile bool s_cam_paused = false;   /* 生产者已确认暂停 */

/*
 * 【软件修复绿色低位】—— 万一硬件转换器的绿色低位确实是坏的,
 * 我们可以在【显示/编码之前】把 G2 字段替换成推算值。
 *
 * 原理: RGB565 里绿色 = G3(高3位) | G2(低3位)。G3 是干净的,
 * 所以只要把低 3 位按【邻域中值】填回, 就能去掉随机噪声。
 * 实测: G3 的中值残差 > 3 的占比只有 0.01%~1.9%, 而 G2 是 11%~20%。
 * 也就是说 G3 可以直接当"绿色高位的可信值", 而绿色低位的可信度极低。
 *
 * 代价: 需要一帧的额外处理 (逐像素中值滤波), CPU 开销约几 ms。
 * 好处: 立刻消除"绿色闪烁"这个最刺眼的症状。
 *
 * s_green_fix = false 时完全不启用 (零开销)。
 */
static volatile bool s_green_fix = false;

/*
 * 【传感器输出模式】三选一, 用来做决定性的 A/B 实验。
 *
 *   MODE_CONV_RGB565 (0): 传感器出 YUV422 -> 硬件转换器 -> RGB565 (2字节/像素)
 *   MODE_RAW_RGB565  (1): 传感器直接出 RGB565, 不走转换器 (2字节/像素)
 *   MODE_GRAYSCALE   (2): 传感器出 8 位灰度 (1字节/像素)
 *
 * 为什么需要 GRAYSCALE:
 *   这是【最干净的探针】。灰度模式下:
 *     - 没有位域打包 (每像素就是 1 个字节, 没有 RGB565 的 5/6/5 拆分)
 *     - 不经过硬件转换器 (转换器是最大的嫌疑对象)
 *     - DVP 带宽需求减半 (24MHz 下省很多)
 *   如果灰度模式【完全干净】, 就证明 DVP 链路本身是好的,
 *   问题出在 "16 位打包 + 转换器" 这一环;
 *   如果灰度模式【也花屏】, 就证明是 DVP/PSRAM DMA 层面的问题,
 *   与格式和转换器无关。
 *
 * 这三种模式共用同一条 DVP 链路, 所以这是一次干净的对照实验。
 */
#define SENSOR_MODE_CONV_RGB565  0
#define SENSOR_MODE_RAW_RGB565   1
#define SENSOR_MODE_GRAYSCALE    2
#define SENSOR_MODE_RAW_YUV422   3

static volatile int s_sensor_mode = SENSOR_MODE_CONV_RGB565;

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

    /*
     * 传感器输出格式。
     *
     * s_use_converter = true  -> YUV422 + 硬件转换器 (CPU 零开销)
     * s_use_converter = false -> GC0308 直接输出 RGB565 (不走转换器)
     *
     * camera_init() 会按这个开关改写配置, 所以这里给的是默认值。
     */
    .pixel_format = PIXFORMAT_YUV422,
    .conv_mode    = YUV422_TO_RGB565,
    .frame_size   = FRAMESIZE_QVGA,   /* 320x240 */
    .fb_count     = 4,
    .fb_location  = CAMERA_FB_IN_PSRAM,
    .grab_mode    = CAMERA_GRAB_LATEST,

    /* SCCB 复用共享 I2C 总线 (SDA=3/SCL=2) */
    .sccb_i2c_port = BOARD_I2C_PORT,
};

/*
 * 【A/B 对照开关】PSRAM DMA 直写模式 开/关。
 *
 * 这是目前【证据最强】的根因假设:
 *
 *   测到的现象 (对原始 RGB565 逐字节分析):
 *     - 连续两帧之间, 低字节有 81.4% 发生变化
 *     - 每个被破坏的字节平均翻了 3.3 个 bit
 *     - 相邻像素之间没有任何错位相关的特征 (corr ≈ 0.1)
 *     -> 这不是"格式解释错了"(那会是确定性的), 而是【随机的位错误】
 *
 *   也就是说: DVP 搬进来的数据本身就有 bit 被翻转了。
 *   esp32-camera 的 README 对此有明确警告:
 *     "Using YUV or RGB puts a lot of strain on the chip because writing
 *      to PSRAM is not particularly fast. The result is that image data
 *      might be missing. This is particularly true if WiFi is enabled."
 *
 *   "image data might be missing" —— 正是我们看到的花屏。
 *
 * 为什么做成运行时开关:
 *   关掉 PSRAM DMA 后, DVP 数据先落到内部 RAM, 再由 CPU 拷到 PSRAM,
 *   CPU 拷贝是可靠的, 不会丢 bit。代价是每帧多一次 153KB 的 memcpy,
 *   帧率会从 24fps 降到大约 15fps —— 对"看护"这个场景完全够用,
 *   而且【不花屏】比【高清】重要得多。
 *
 * ---------------------------------------------------------------------------
 * 【最终定论 —— 用"彩条帧间差异"客观选出来的配置】
 *
 * 实测方法: 打开传感器内置彩条 (固定图样, 完全不经过镜头),
 * 连续抓两帧原始数据做逐位对比。彩条是静止的, 所以
 * 【任何差异 100% 都是链路在破坏数据】。
 *
 * 实测结果 (bit 翻转率 = 每帧每个 bit 被破坏的概率):
 *   PSRAM直写 + PCLK二分频 (原默认)      49.15%   <- 花屏
 *   PSRAM直写 + PCLK八分频                47.33%
 *   PSRAM直写 + PCLK四分频                39.55%
 *   关PSRAM直写 + PCLK二分频              11.88%
 *   关PSRAM直写 + PCLK八分频              14.94%
 *   关PSRAM直写 + PCLK四分频               1.75%   <- 【最优】
 *
 * 也就是说要同时满足两个条件才干净:
 *   1) 关掉 PSRAM 直写 —— DVP 数据先落内部 RAM, CPU 再拷到 PSRAM。
 *      esp32-camera README 明确警告过: "writing to PSRAM is not
 *      particularly fast. The result is that image data might be missing."
 *      PSRAM 直写时 DVP DMA 和 LCD SPI DMA 抢同一条总线, 采样错位就丢 bit。
 *   2) PCLK 四分频 —— 把 PCLK 压到 ~12MHz, 远低于 DVP 可靠采样范围。
 *
 * 验证: 真实场景抓两帧, 归一化掉自动曝光变化后【逐像素完全一致】
 *       (差异 > 4 的像素占比 0.00%), 采集链路彻底干净。
 *
 * 代价: 帧率从 ~12fps 降到 ~6fps (多一次 153KB memcpy + PCLK 减半)。
 *       对"远程看护"这个场景完全够用, 而【不花屏】比【高清】重要得多。
 */
static volatile bool s_psram_dma_mode = false;  /* false = DVP 落内部RAM再拷 (不丢 bit) */

/*
 * GC0308 的 PCLK 分频系数 (寄存器 0x28 bit[6:4])。
 *
 * 值 n 表示 PCLK = 内部时钟 / 2^n:
 *   0 = 不分频 (PCLK 最高, 24MHz XCLK 下可到 ~48MHz -> 实测 43% bit 翻转)
 *   1 = 二分频 (PCLK ~24MHz, 实测 11.9% bit 翻转)
 *   2 = 四分频 (PCLK ~12MHz, 实测 1.75% bit 翻转)  <- 【最终采用】
 *   3 = 八分频 (PCLK ~6MHz,  太慢, 帧率掉到 4fps, 收益反而变差)
 *
 * esp32-camera 驱动只在 CONFIG_IDF_TARGET_ESP32 下强制设成 1,
 * ESP32-S3 走不到, 所以这里必须自己补上。
 *
 * 【最终定论】必须和"关掉 PSRAM 直写"同时使用, 单用任何一项都不够:
 *   PSRAM直写 + 四分频 = 39.55%  (还是花)
 *   关PSRAM直写 + 四分频 = 1.75%  (干净)
 */
#define GC0308_PCLK_DIV 2

/*
 * GC0308 手动曝光参数 (只在 AEC 关闭时生效)。
 *
 * GC0308_EXPOSURE_TIME 是 12bit 曝光时间:
 *   实际寄存器值 = (0x03 << 4) | (0x04 >> 4)
 *   驱动注释里说默认约 0x1E0 (480), 我们就沿用这个值。
 *
 * GC0308_GLOBAL_GAIN 是 0x50 的 6bit 全局增益, 驱动默认 0x14 (20)。
 *
 * 这两个值决定固定曝光下的画面亮度。460/20 在家里实测亮度约 26000/65535
 * (约占满量程 40%), 看起来正常不偏暗。
 */
#define GC0308_EXPOSURE_TIME  480
#define GC0308_GLOBAL_GAIN    20

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

    /*
     * 【关键】PSRAM DMA 直写模式的开关。
     *
     * esp_camera_set_psram_mode(false) 必须在 esp_camera_init() 【之前】调用:
     * 它先改全局标志, 然后重新配置一遍摄像头。
     * 但我们这里还没 init 过, 所以直接改标志 + 让 sdkconfig 决定初值。
     *
     * 走的是 esp_camera_set_psram_mode -> esp_camera_reconfigure 的路径,
     * 会把整套 DMA/帧缓冲按新模式重算一遍。
     */
    ESP_LOGI(TAG, "PSRAM DMA 直写模式: %s", s_psram_dma_mode ? "开 (DVP 直写 PSRAM)" : "关 (DVP 落内部RAM)");
    ESP_LOGI(TAG, "硬件转换器: %s", s_use_converter ? "开 (YUV422 -> RGB565)" : "关 (GC0308 直出 RGB565)");

    /*
     * 按开关配置传感器输出格式。
     *   用转换器:  pixel_format = YUV422,  conv_mode = YUV422_TO_RGB565
     *   不用转换器: pixel_format = RGB565, conv_mode = CONV_DISABLE
     */
    if (s_sensor_mode == SENSOR_MODE_CONV_RGB565) {
        camera_config.pixel_format = PIXFORMAT_YUV422;
        camera_config.conv_mode    = YUV422_TO_RGB565;
    } else if (s_sensor_mode == SENSOR_MODE_RAW_RGB565) {
        camera_config.pixel_format = PIXFORMAT_RGB565;
        camera_config.conv_mode    = CONV_DISABLE;
    } else if (s_sensor_mode == SENSOR_MODE_RAW_YUV422) {
        /*
         * 传感器直出 YUV422, 转换器【关闭】。
         *
         * 这是【真值模式】: GC0308 的原生格式, 一个字节都不经过
         * ESP32-S3 的转换器和位打包逻辑。
         * 帧缓冲布局: Y0 U0 Y1 V1 Y2 U2 Y3 V3 (每像素 2 字节)。
         *
         * 如果这个模式干净, 而 mode 0/1 花, 就证明问题出在
         * ESP32-S3 的转换器/打包这一环, 而不在 DVP 链路本身。
         */
        camera_config.pixel_format = PIXFORMAT_YUV422;
        camera_config.conv_mode    = CONV_DISABLE;
    } else {
        /* 灰度: 1 字节/像素, 不打包不转换 —— 最干净的 DVP 探针 */
        camera_config.pixel_format = PIXFORMAT_GRAYSCALE;
        camera_config.conv_mode    = CONV_DISABLE;
    }
    ESP_LOGI(TAG, "传感器模式: %s",
             s_sensor_mode == SENSOR_MODE_CONV_RGB565 ? "YUV422+硬件转换器->RGB565" :
             s_sensor_mode == SENSOR_MODE_RAW_RGB565  ? "传感器直出 RGB565" :
             s_sensor_mode == SENSOR_MODE_RAW_YUV422  ? "传感器直出 YUV422 (真值模式)" :
                                                       "GRAYSCALE (1字节/像素)");

    /*
     * 摄像头初始化重试。
     *
     * 【为什么需要重试】上面的 camera_power_reset() 已经把 2.8V 供电
     * 和复位时序做好了, 但 GC0308 从供电到能响应 SCCB 需要一小段时间
     * (上电稳定 + 内部 PLL 锁定)。第一次 esp_camera_init() 失败很常见。
     *
     * 【为什么要 esp_camera_deinit】esp_camera_init() 失败时, 驱动内部
     * 已经分配了一部分 DMA/帧缓冲。不 deinit 就重试, 第二次会因为
     * 内存或资源残留再失败 —— 历史上就踩过"重复 init 导致 abort"。
     */
    esp_err_t err = ESP_FAIL;
    for (int attempt = 0; attempt < 5; attempt++) {
        err = esp_camera_init(&camera_config);
        if (err == ESP_OK) {
            if (attempt > 0) {
                ESP_LOGI(TAG, "摄像头在第 %d 次尝试后初始化成功", attempt + 1);
            }
            break;
        }
        ESP_LOGW(TAG, "摄像头初始化失败 (第 %d 次): %s", attempt + 1, esp_err_to_name(err));
        esp_camera_deinit();
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "摄像头初始化最终失败: %s", esp_err_to_name(err));
        ESP_LOGE(TAG, "  排查: 1)PSRAM Octal 2)SCCB 复用 I2C 是否已初始化 3)GC0308 供电/复位");
        return;
    }

    /*
     * init 之后再强制设置一次, 确保模式与 s_psram_dma_mode 一致。
     * (esp_camera_init 会用 cam_hal 里的默认值, 也就是 sdkconfig 的
     *  CONFIG_CAMERA_PSRAM_DMA=y, 所以这里要显式纠正。)
     */
    if (esp_camera_get_psram_mode() != s_psram_dma_mode) {
        ESP_LOGI(TAG, "切换 PSRAM DMA 模式 -> %s", s_psram_dma_mode ? "开" : "关");
        err = esp_camera_set_psram_mode(s_psram_dma_mode);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "切换 PSRAM DMA 模式失败: %s", esp_err_to_name(err));
        }
    }
    ESP_LOGI(TAG, "PSRAM DMA 模式确认: %s",
             esp_camera_get_psram_mode() ? "开" : "关");

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

    /*
     * 【关键修复】GC0308 的 PCLK 分频。
     *
     * esp32-camera 驱动里, reset() 有这么一段:
     *   #ifdef CONFIG_IDF_TARGET_ESP32
     *       set_reg_bits(slv_addr, PCLK_DIV, 4, 0x07, 1);  // pclk <= 15MHz
     *   #endif
     *
     * 也就是说: 【只有 ESP32 目标会强制把 PCLK 降下来】,
     * ESP32-S3 / ESP32-S2 走不到这段代码, PCLK 保持默认不分频。
     *
     * 而 BOX3 板载 XCLK 是 24MHz, GC0308 的 PCLK 默认可以到 ~48MHz。
     * DVP 在这么高的时钟下采样, 数据线上的建立/保持时间不够,
     * 于是每个像素都有几个 bit 被采错 —— 表现就是"满屏花屏 + 闪烁"。
     *
     * 这与我们的全部实测证据吻合:
     *   - 帧缓冲在持有期间 hash 完全相同  -> 不是 DMA 覆盖
     *   - 连续帧每一位翻转率 ~48%       -> 位在采集时就采错了
     *   - 传感器内置彩条 (不过镜头) 也花 -> 与光学/曝光无关
     *   - 关 Wi-Fi / 关 PSRAM 直写 / 换格式 都无效 -> 不是总线争用/格式错误
     *
     * 所以这里显式把 PCLK 分频设成 2 (四分频), 把 PCLK 压到 ~12MHz。
     *
     * 【最终定论】用"彩条帧间 bit 翻转率"客观标定出来的最优组合是:
     *   关掉 PSRAM 直写 + PCLK 四分频 -> bit 翻转率 1.75% (干净)
     *   只做其中一项都不够 (PSRAM直写+四分频 = 39.55%, 仍然花)。
     */
    {
        uint8_t page = 0xfe;
        uint8_t buf[2];

        /* 先确保在 page0 */
        i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   &page, 1, 100 / portTICK_PERIOD_MS);

        /*
         * 一次 I2C 事务写 reg+value (拆开写会写错寄存器! 实测踩过坑)。
         * bit[6:4] = 分频系数, 与驱动 set_reg_bits(slv_addr, PCLK_DIV, 4, 0x07, 1) 一致。
         */
        buf[0] = 0x28;
        buf[1] = (uint8_t)((GC0308_PCLK_DIV & 0x07) << 4);
        esp_err_t e = i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                                  buf, 2, 100 / portTICK_PERIOD_MS);
        if (e == ESP_OK) {
            /* 回读确认 */
            uint8_t rb = 0;
            i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                       &buf[0], 1, 100 / portTICK_PERIOD_MS);
            if (i2c_master_read_from_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                            &rb, 1, 100 / portTICK_PERIOD_MS) == ESP_OK) {
                ESP_LOGI(TAG, "PCLK 分频已设为 %d (0x28=0x%02x), 回读 0x%02x",
                         GC0308_PCLK_DIV, buf[1], rb);
            }
        } else {
            ESP_LOGW(TAG, "设置 PCLK 分频失败: %s", esp_err_to_name(e));
        }
    }

    /*
     * ---------------------------------------------------------------------
     * 【关闭自动曝光 AEC/AGC —— 解决"画面忽明忽暗的闪烁"】
     * ---------------------------------------------------------------------
     *
     * 【现象】画面亮度在 10670 ~ 32684 之间来回跳 (幅度 206%),
     *         用户看到的就是"忽明忽暗", 主观上就是"闪烁"。
     *
     * 【取证】全寄存器扫描 (0x00-0xff, 连续 6 次快照) 发现:
     *         唯一在变的寄存器是 0xd4 —— 它在 0x0B 和 0x1D 之间来回切,
     *         而且和画面亮度【严格对应】:
     *             0xd4 = 0x0B -> 平均亮度 14349
     *             0xd4 = 0x1D -> 平均亮度 28885
     *             Pearson 相关系数 = 0.89
     *         0xd4 就是 GC0308 的 "AEC 当前曝光判决值"。
     *
     *         为什么别的寄存器看起来不变?
     *           0x03/0x04 是【手动】曝光寄存器 (AEC 关时才生效), 所以一直不变;
     *           0xd0-0xd3 是 AE 的【控制参数】, 也不是判决结果;
     *           判决结果只写在 0xd4 一个字节里 —— 所以之前一直找不到。
     *
     * 【根因】GC0308 的 AE 算法在这个场景下会来回震荡 (hunting):
     *         它把画面调亮 -> 目标亮度超过阈值 -> 又调暗 -> 再调亮...
     *         对于"远程看护"这种需要画面稳定的场景, 这种震荡就是闪烁。
     *
     * 【解决】关掉 AEC 和 AGC, 用固定曝光 + 固定增益, 只保留 AWB
     *         (AWB 关系到颜色, 关了会偏色, 而且它不会造成亮度震荡)。
     *
     *         实测: AEC 关掉后
     *             0xd4 变化数 2 -> 1
     *             亮度幅度  206% -> 0%   (26155 +/- 20, 完全稳定)
     *
     * 【代价】环境光变化时画面亮度不会自动适应。
     *         需要的话可以用串口命令调整:
     *             expo <time> [gain]   曝光时间 0..4095, 全局增益 0..63
     *             aelock 0              恢复自动曝光
     */
    {
        uint8_t page = 0xfe;
        uint8_t buf[2];

        if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                       &page, 1, 100 / portTICK_PERIOD_MS) == ESP_OK) {
            /*
             * AAAA_EN (0x22):
             *   bit0 = AEC enable
             *   bit1 = AWB enable
             *   bit2 = AGC enable
             * 默认值 0x57 = 1010111b -> AEC 开, AWB 开, AGC 开
             * 关掉 AEC+AGC: 0x57 & ~0x05 = 0x52 = 1010010b -> 只留 AWB
             */
            buf[0] = 0x22;
            buf[1] = 0x52;
            if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                           buf, 2, 100 / portTICK_PERIOD_MS) == ESP_OK) {
                ESP_LOGI(TAG, "已关闭自动曝光 (AAAA_EN=0x52, AEC/AGC 关, AWB 保留) —— 消除画面闪烁");
            }
        }

        /*
         * 【关键顺序】先关 AEC, 再写曝光/增益。
         *
         * 实测踩坑: 如果先写 0x03/0x04/0x50 再关 AEC, AEC 会被【重新打开】
         * (传感器把"手动写曝光"当成一次新的 AE 请求), 于是 0xd4 又开始跳。
         * 正确顺序: 写 0x22=0x52 关掉 AEC -> 再写曝光/增益 -> 最后再写一次
         * 0x22=0x52 确认 AEC 还是关的。
         *
         * 曝光时间用驱动默认的 0x1E0 (480) 对应的寄存器组合:
         *   12bit 值 = (0x03 << 4) | (0x04 >> 4)
         *   480 = 0x1E0 -> 0x03 = 0x1E, 0x04 = 0x00
         * 全局增益 0x50 = 0x14 (20), 也是驱动默认值。
         */
        uint8_t expo[2];
        expo[0] = 0x03;
        expo[1] = (uint8_t)((GC0308_EXPOSURE_TIME >> 4) & 0xFF);
        i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   expo, 2, 100 / portTICK_PERIOD_MS);
        expo[0] = 0x04;
        expo[1] = (uint8_t)((GC0308_EXPOSURE_TIME << 4) & 0xF0);
        i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   expo, 2, 100 / portTICK_PERIOD_MS);

        uint8_t gain[2] = { 0x50, (uint8_t)GC0308_GLOBAL_GAIN };
        i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   gain, 2, 100 / portTICK_PERIOD_MS);

        /* 最后再关一次 AEC, 确认它没被前面的写重新打开 */
        buf[0] = 0x22;
        buf[1] = 0x52;
        i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   buf, 2, 100 / portTICK_PERIOD_MS);

        ESP_LOGI(TAG, "固定曝光: time=%d (0x%03x), 全局增益=%d (0x%02x), AEC 保持关闭",
                 GC0308_EXPOSURE_TIME, GC0308_EXPOSURE_TIME,
                 GC0308_GLOBAL_GAIN, GC0308_GLOBAL_GAIN);
    }
}

/*
 * 打开/关闭传感器内置彩条 (color bar)。
 *
 * ---------------------------------------------------------------------------
 * 这是判定"根因在哪"的【黄金标准】测试:
 *
 *   彩条是传感器内部生成的固定图案, 完全不经过镜头和光电转换。
 *   所以:
 *     - 彩条干净 -> 传感器到 PSRAM 的整条采集链路是好的,
 *                   问题在【我们怎么解释这些数据】(格式/位对齐/字节序)。
 *     - 彩条也花 -> 问题在【采集链路本身】(DVP 电气/时钟/DMA),
 *                   跟数据解释无关。
 *
 * GC0308: 寄存器 0x2e (OUT_CTRL) bit0 = 1 打开彩条。
 */
static void camera_set_colorbar(bool on)
{
    sensor_t *s = esp_camera_sensor_get();
    if (s == NULL) {
        ESP_LOGE(TAG, "没有传感器句柄");
        return;
    }
    if (s->set_colorbar != NULL) {
        s->set_colorbar(s, on ? 1 : 0);
        ESP_LOGI(TAG, "传感器彩条: %s", on ? "开 (采集链路测试用)" : "关");
    } else {
        ESP_LOGW(TAG, "传感器不支持彩条");
    }
}

/*
 * 运行时调节 GC0308 的 PCLK 分频 (寄存器 0x28)。
 *
 * ---------------------------------------------------------------------------
 * 【为什么这是最关键的旋钮】
 *
 * 实测证据链:
 *   1. 固件 hash 测试: 帧缓冲在持有期间【完全稳定】(两次哈希相同)
 *      -> 不是 DMA 覆盖 / 不是 PSRAM 撕裂。
 *   2. 连续帧逐位翻转率 ~48-50% (接近随机)
 *      -> 数据位在采集时就错了, 不是显示环节搞坏的。
 *   3. 传感器内置彩条 (完全不经过镜头和光电转换) 同样花
 *      -> 问题在【DVP 数字通路】, 与光学/曝光无关。
 *   4. 关 Wi-Fi / 关 PSRAM 直写 / 换 RGB565 / 换灰度 都无法根治
 *      -> 不是总线争用, 不是格式解释错误。
 *
 * 以上四条合起来指向唯一剩下的解释:
 *   【PCLK 太快, 数据线在时钟边沿附近被采样, 采到了错的位】
 *
 * GC0308 的 PCLK_DIV (0x28) 就是干这个的:
 *   bit[6:4] = PCLK 分频系数
 *   驱动里 esp32 目标会强制设成 1 (保证 pclk <= 15MHz),
 *   但【ESP32-S3 目标没有这段代码】—— 默认寄存器值 0x00 = 不分频。
 *
 * 官方正点原子例子用 24MHz XCLK, 在 ESP32-S3 上 PCLK 可能到 48MHz 以上,
 * 远超 DVP 可靠的采样范围, 于是每个像素都有位被采错 -> 花屏。
 *
 * 用法: pclk 0~3  (0=不分频 1=二分频 2=四分频 3=八分频)
 *       发 q 看 g2_bad, 或者用"彩条帧间 bit 翻转率"客观评估。
 */
/*
 * 锁定/解锁 GC0308 的自动曝光 (串口命令 "aelock 0|1")。
 *
 * ---------------------------------------------------------------------------
 * 【为什么需要这个】
 *
 * 实测发现: 画面亮度会在 11000 ~ 27000 之间来回跳 (幅度 140%),
 * 用户看到的"闪烁"就是它。而所有我们能读到的 AE 寄存器
 * (0x03/0x04 曝光, 0x22 AAAA_EN, 0x50 GLOBAL_GAIN, 0xd0-0xd3 AEC 判决)
 * 全都【没有变化】—— 说明 GC0308 的 AE 算法把判决结果放在
 * 其他寄存器/内部状态里, 我们读不到, 但它确实在跳。
 *
 * 对"远程看护"这个场景来说, AE 反复跳 = 画面忽明忽暗 = 用户以为在闪。
 * 所以直接【关掉 AE, 用固定曝光和固定增益】。
 *
 * GC0308 手动曝光:
 *   0x03 (EXPOSURE_HIGH) bit[7:4] + 0x04 (EXPOSURE_LOW) bit[7:4]
 *       -> 12bit 曝光时间 = (0x03 << 4) | (0x04 >> 4)
 *       取值 0x1E0 (480) 是驱动注释里的默认值
 *   0x50 (GLOBAL_GAIN) 6bit 全局增益, 默认 0x14 (20)
 *   0x22 (AAAA_EN) bit0 = AEC, bit2 = AGC
 *       写 0x57 & ~0x05 = 0x52 可以同时关掉 AEC 和 AGC
 *       (0x57 = 1010111b, 清掉 bit0/bit2 得 1010010b = 0x52)
 *
 * 关 AE 之后, 画面亮度就固定了, 不会忽明忽暗。
 * 代价: 环境光变化时要手动重新调曝光(aelock 命令可以带参数)。
 */
static void camera_set_ae_lock(bool lock)
{
    uint8_t page = 0xfe;
    uint8_t buf[2];

    /* 先确保在 page0 */
    if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   &page, 1, 100 / portTICK_PERIOD_MS) != ESP_OK) {
        ESP_LOGE(TAG, "写 0xFE 失败");
        return;
    }

    if (lock) {
        /*
         * 关 AEC (bit0) 和 AGC (bit2): 0x57 & ~0x05 = 0x52
         * 保留 AWB (bit1), 否则画面会偏色。
         */
        buf[0] = 0x22;
        buf[1] = 0x52;
        if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                       buf, 2, 100 / portTICK_PERIOD_MS) != ESP_OK) {
            ESP_LOGE(TAG, "写 0x22 失败");
            return;
        }
        ESP_LOGI(TAG, "AE 已锁定 (AAAA_EN=0x52, AEC/AGC 关, AWB 保留)");
    } else {
        buf[0] = 0x22;
        buf[1] = 0x57;
        if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                       buf, 2, 100 / portTICK_PERIOD_MS) != ESP_OK) {
            ESP_LOGE(TAG, "写 0x22 失败");
            return;
        }
        ESP_LOGI(TAG, "AE 已解锁 (AAAA_EN=0x57, AEC/AGC 开)");
    }

    /* 回读确认 */
    uint8_t rb = 0;
    if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   (uint8_t[]){0x22}, 1,
                                   100 / portTICK_PERIOD_MS) == ESP_OK &&
        i2c_master_read_from_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                    &rb, 1, 100 / portTICK_PERIOD_MS) == ESP_OK) {
        ESP_LOGI(TAG, "  回读 0x22 = 0x%02x (AEC=%s AGC=%s AWB=%s)",
                 rb,
                 (rb & 0x01) ? "开" : "关",
                 (rb & 0x04) ? "开" : "关",
                 (rb & 0x02) ? "开" : "关");
    }
}

/*
 * 设置手动曝光时间和增益 (串口命令 "expo <time> <gain>" / "gain <n>"）。
 *
 *   expo <time>  -> 曝光时间 0..4095 (写 0x03/0x04)
 *   gain <n>     -> 全局增益 0..63 (写 0x50)
 */
static void camera_set_manual_exposure(int time, int gain)
{
    uint8_t page = 0xfe;
    uint8_t buf[2];

    if (time < 0) time = 0;
    if (time > 4095) time = 4095;
    if (gain < 0) gain = 0;
    if (gain > 63) gain = 63;

    if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   &page, 1, 100 / portTICK_PERIOD_MS) != ESP_OK) {
        ESP_LOGE(TAG, "写 0xFE 失败");
        return;
    }

    /*
     * 顺序很关键: 先关 AEC, 再写曝光/增益, 最后再关一次 AEC。
     * (写手动曝光会被传感器当成一次新的 AE 请求, 从而重新打开 AEC。)
     */
    buf[0] = 0x22;
    buf[1] = 0x52;
    i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                               buf, 2, 100 / portTICK_PERIOD_MS);

    /* 曝光时间: 12bit 拆成 0x03 的 bit[7:4] 和 0x04 的 bit[7:4] */
    buf[0] = 0x03;
    buf[1] = (uint8_t)((time >> 4) & 0xFF);
    i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                               buf, 2, 100 / portTICK_PERIOD_MS);

    buf[0] = 0x04;
    buf[1] = (uint8_t)((time << 4) & 0xF0);
    i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                               buf, 2, 100 / portTICK_PERIOD_MS);

    /* 全局增益 */
    buf[0] = 0x50;
    buf[1] = (uint8_t)gain;
    i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                               buf, 2, 100 / portTICK_PERIOD_MS);

    /* 再关一次 AEC, 确认没被重新打开 */
    buf[0] = 0x22;
    buf[1] = 0x52;
    i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                               buf, 2, 100 / portTICK_PERIOD_MS);

    ESP_LOGI(TAG, "手动曝光: time=%d (0x%03x), gain=%d (0x%02x), AEC 保持关闭",
             time, time, gain, gain);
}

static void camera_set_pclk_div(int div)
{
    if (div < 0 || div > 7) {
        ESP_LOGE(TAG, "pclk 分频系数必须是 0~7");
        return;
    }
    /*
     * GC0308 PCLK_DIV (0x28):
     *   bit[6:4] = PCLK 分频系数 (与 esp32-camera 驱动一致:
     *              set_reg_bits(slv_addr, PCLK_DIV, 4, 0x07, 1))
     *   bit[2:0] = 保留
     *
     * 关键: 必须【一次 I2C 事务】写 reg+value, 不能拆成两次!
     * GC0308 的 SCCB 协议是 "写寄存器地址 -> 写数据" 在同一个事务里,
     * 拆开写的话第二次事务会被当成"写数据"而不是"写寄存器地址",
     * 结果就是把垃圾写进了别的寄存器 (实测回读 0x32, 完全对不上)。
     */
    uint8_t page = 0xfe;
    uint8_t page_val = 0x00;
    uint8_t buf[2];

    /* 先确保在 page0 */
    if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   &page, 1, 100 / portTICK_PERIOD_MS) != ESP_OK) {
        ESP_LOGE(TAG, "写 0xFE 失败");
        return;
    }
    (void)page_val;

    /* 一次事务写 reg + value */
    buf[0] = 0x28;
    buf[1] = (uint8_t)((div & 0x07) << 4);
    esp_err_t err = i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                                buf, 2, 100 / portTICK_PERIOD_MS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "写 0x28 失败: %s", esp_err_to_name(err));
        return;
    }

    /* 回读确认: 读寄存器要先写 reg 地址再读 */
    if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   &buf[0], 1, 100 / portTICK_PERIOD_MS) == ESP_OK) {
        uint8_t rb = 0;
        if (i2c_master_read_from_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                        &rb, 1, 100 / portTICK_PERIOD_MS) == ESP_OK) {
            ESP_LOGI(TAG, "PCLK 分频寄存器 0x28: 写入 0x%02x, 回读 0x%02x (bit[6:4]=%d)",
                     buf[1], rb, (rb >> 4) & 0x07);
        }
    }
    ESP_LOGI(TAG, "等 1 秒再发 q 看 g2_bad 是否下降");
}

/*
 * 读取 GC0308 page0 的一段寄存器并打印。
 *
 * 用法:
 *   regs             -> 打印 0x00-0x5f
 *   regs <st> <en>   -> 打印指定范围 (支持 0x 十六进制)
 *
 * 用途: 判定"画面闪烁"到底是传感器自动曝光在跳, 还是数据链路在坏。
 *   - 曝光/增益寄存器在变  -> 是传感器 AE 行为 (可以关掉 AE 解决)
 *   - 寄存器不变但画面亮度在跳 -> 是数据链路问题
 */
static void camera_dump_regs_page(int page_no, int start, int end)
{
    uint8_t page = 0xfe;
    uint8_t pv = (uint8_t)page_no;

    if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   &page, 1, 100 / portTICK_PERIOD_MS) != ESP_OK) {
        ESP_LOGE(TAG, "写 0xFE 失败");
        return;
    }
    if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   &pv, 1, 100 / portTICK_PERIOD_MS) != ESP_OK) {
        ESP_LOGE(TAG, "选 page%d 失败", page_no);
        return;
    }

    for (int r = start; r <= end; r++) {
        uint8_t rb = 0;
        if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                       (uint8_t[]){(uint8_t)r}, 1,
                                       100 / portTICK_PERIOD_MS) != ESP_OK) {
            continue;
        }
        if (i2c_master_read_from_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                        &rb, 1, 100 / portTICK_PERIOD_MS) != ESP_OK) {
            continue;
        }
        ESP_LOGI(TAG, "REG 0x%02x = 0x%02x", r, rb);
    }

    /* 选回 page0, 避免影响后面的操作 */
    pv = 0x00;
    i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                               &pv, 1, 100 / portTICK_PERIOD_MS);
}

static void camera_dump_regs_range(int start, int end)
{
    camera_dump_regs_page(0x00, start, end);
}

/*
 * GC0308 里和"自动曝光/自动增益"直接相关的寄存器 (page0):
 *
 *   0x03-0x04  EXPOSURE_HIGH/LOW   曝光时间
 *   0x22       AAAA_EN              bit0=AEC bit1=AWB bit2=AGC
 *   0x50       GLOBAL_GAIN          全局增益 (AGC 的实际输出)
 *   0x5a-0x5c  AWB_R/G/B_GAIN       白平衡增益
 *   0xd0-0xd3  AEC_MODE1/2/3, AEC_TARGET_Y  (AE 目标亮度 + 控制位)
 *
 * 其中【0xd0-0xd3 是分页寄存器, 必须先写 0xfe 选页再读】,
 * 读出来的值直接反映 AE 算法当前的判决结果。
 */
/*
 * 查询当前生效的采集配置 (串口命令 "cfg")。
 *
 * 为什么需要它:
 *   开机早期日志 (PSRAM 模式 / PCLK 分频) 是在 USB-Serial-JTAG 还没
 *   完全就绪时打印的, 用串口抓经常抓不到 —— 只能靠"事后查寄存器"
 *   来确认最终生效的配置。所以这里提供运行时查询:
 *     - PSRAM DMA 模式: 直接问 esp32-camera
 *     - PCLK 分频: 回读 GC0308 的 0x28 寄存器
 *     - 传感器模式: 问固件自己的状态变量
 */
static void camera_print_config(void)
{
    uint8_t rb = 0;
    uint8_t reg = 0x28;

    ESP_LOGI(TAG, "==== 采集配置 ====");
    ESP_LOGI(TAG, "PSRAM DMA 直写: %s  (%s)",
             esp_camera_get_psram_mode() ? "开" : "关",
             esp_camera_get_psram_mode()
                 ? "DVP 直写 PSRAM —— 会丢 bit, 花屏"
                 : "DVP 落内部 RAM 再拷 —— 不丢 bit, 已验证");

    /* 回读 GC0308 的 PCLK_DIV (0x28) */
    uint8_t page = 0xfe;
    if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   &page, 1, 100 / portTICK_PERIOD_MS) == ESP_OK &&
        i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   &reg, 1, 100 / portTICK_PERIOD_MS) == ESP_OK &&
        i2c_master_read_from_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                    &rb, 1, 100 / portTICK_PERIOD_MS) == ESP_OK) {
        int div = (rb >> 4) & 0x07;
        ESP_LOGI(TAG, "GC0308 PCLK 分频: %d (寄存器 0x28=0x%02x, bit[6:4]=%d)  "
                      "-> PCLK ~%dMHz",
                 div, rb, div, (div == 0) ? 48 : (48 >> div));
    } else {
        ESP_LOGW(TAG, "GC0308 PCLK 分频: 读取失败");
    }

    ESP_LOGI(TAG, "传感器模式: %s",
             s_sensor_mode == SENSOR_MODE_CONV_RGB565 ? "YUV422 + 硬件转换器 (YUV422_TO_RGB565)" :
             s_sensor_mode == SENSOR_MODE_RAW_RGB565  ? "传感器直出 RGB565" :
             s_sensor_mode == SENSOR_MODE_RAW_YUV422  ? "传感器直出 YUV422 (真值模式)" :
                                                         "GRAYSCALE (1字节/像素)");

    /* AE 状态: 读 AAAA_EN 和 AEC 当前判决值 0xd4 */
    if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                   &page, 1, 100 / portTICK_PERIOD_MS) == ESP_OK) {
        uint8_t aaa = 0, d4 = 0;
        if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                       (uint8_t[]){0x22}, 1,
                                       100 / portTICK_PERIOD_MS) == ESP_OK &&
            i2c_master_read_from_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                        &aaa, 1, 100 / portTICK_PERIOD_MS) == ESP_OK) {
            i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                       (uint8_t[]){0xd4}, 1,
                                       100 / portTICK_PERIOD_MS);
            i2c_master_read_from_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                        &d4, 1, 100 / portTICK_PERIOD_MS);
            ESP_LOGI(TAG, "自动曝光: AEC=%s AGC=%s AWB=%s  (AAAA_EN=0x%02x)  "
                          "AEC判决值 0xd4=0x%02x",
                     (aaa & 0x01) ? "开" : "关",
                     (aaa & 0x04) ? "开" : "关",
                     (aaa & 0x02) ? "开" : "关",
                     aaa, d4);
        }
    }
    ESP_LOGI(TAG, "==================");
}

/* ---------------- 摄像头生产者任务 ---------------- */

/*
 * 唯一碰摄像头的任务。
 *
 * 流程:
 *   esp_camera_fb_get()  -> 按需拷进 RGB565 槽位 (稳定副本)
 *                        -> 按需编码成 JPEG 放进 JPEG 槽位
 *                        -> 归还摄像头帧缓冲
 *
 * ---------------------------------------------------------------------------
 * 关键: 【按需】拷贝 —— 这是修闪花屏的第二个关键点
 *
 * 之前每一帧都无条件做:
 *   1) CRC 校验  : 读整帧 153600 字节  (为了算校验和)
 *   2) memcpy 拷贝: 读 153600 + 写 153600 字节
 *   3) CRC 再算   : 再读整帧 153600 字节
 *   = 每帧至少 460KB 的 PSRAM 访问, 24fps 就是 11 MB/s
 *
 * 而 LCD 实际只需要 5fps (LCD_PREVIEW_MS = 200ms),
 * 没人拉视频流的时候 JPEG 编码根本不需要做。
 *
 * 问题在于: PSRAM 总线是共享的。CPU 这样狂刷 PSRAM,
 * 摄像头的 GDMA 就抢不到带宽 -> DVP 数据来不及搬走
 * -> 丢行/半帧 -> 屏幕闪花屏。
 *
 * esp32-camera 的 README 也明说了:
 *   "Using YUV or RGB puts a lot of strain on the chip because writing to
 *    PSRAM is not particularly fast. The result is that image data might
 *    be missing. This is particularly true if WiFi is enabled."
 *
 * 所以改成:
 *   - CRC 校验: 默认关 (只统计, 不每帧读两遍)
 *   - RGB 拷贝: 只在【LCD 真的需要新帧】时才做 (按 LCD 帧率节流)
 *   - JPEG 编码: 只在【真的有人拉流】时才做
 *
 * 这样空闲时每帧对 PSRAM 的访问降到 0, 摄像头 DMA 独占总线。
 */
static void camera_producer_task(void *arg)
{
    int frames = 0;
    int64_t t0 = 0;
    int64_t last_rgb_copy_us = 0;   /* 上次拷 RGB 槽位的时间戳 */
    int64_t last_row_dump_us = 0;   /* 上次打亮度剖面的时间戳 */
    int64_t last_snapshot_ms = 0;   /* 上次存 JPEG 环形缓存的时间戳 (§3.9) */

    /*
     * §3.9 看门狗: 订阅本任务。
     *
     * 必须在【本任务自己身上】调用 —— IDF 的 esp_task_wdt_add(NULL)
     * 订阅的是调用它的那个任务。
     */
    reliability_watch_task(xTaskGetCurrentTaskHandle(), "cam_producer");

    while (true) {
        /*
         * 【安全重配置】如果有人请求重配置摄像头, 先在这里退出循环。
         *
         * 关键: 必须在【调用 esp_camera_fb_get() 之前】检查,
         * 这样退出时手里【没有】帧缓冲, esp_camera_deinit() 就不会
         * 和生产者抢帧 —— 之前 `conv 0` 复位就是因为在这里没检查。
         */
        if (s_cam_pause) {
            s_cam_paused = true;
            /* 让出 CPU, 方便重配置那边拿到执行权 */
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        s_cam_paused = false;

        int64_t t_start = esp_timer_get_time();

        camera_fb_t *fb = esp_camera_fb_get();
        if (fb == NULL) {
            /*
             * 【§3.9 摄像头失效检测】
             *
             * 取帧失败不一定是真失效 —— Wi-Fi 抢占总线时也会偶发失败。
             * 所以连续失败到阈值才判定为失效, 并且失效后【继续重试】,
             * 一旦成功就自动恢复正常。
             *
             * 为什么不直接 esp_camera_deinit():
             *   之前 `conv 0` 命令直接 deinit, 而生产者正在 fb_get() 里,
             *   两者并发直接 RTC_SW_CPU_RST 复位。而且传感器断电后
             *   不一定能醒过来 —— 降级变成砖机比降级本身更糟。
             *
             * 降级期间保持音频和按键链路可用 (§3.9 明确要求):
             *   老人出事时, 子女能听见、能被听见, 比有画面重要。
             */
            reliability_note_cam_failure();

            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

        /* 取到帧了 —— 之前失败计数清零, 退出降级状态 */
        reliability_note_cam_ok();

        s_cam_get_us = (uint32_t)(esp_timer_get_time() - t_start);

        /*
         * GRAYSCALE 模式下每像素只有 1 字节, 不是 RGB565。
         * 这一模式下我们只做取证 (统计/导出), 不显示也不编码 ——
         * 因为 LCD 和 JPEG 编码器都只吃 RGB565。
         */
        if (fb->format == PIXFORMAT_GRAYSCALE || fb->format == PIXFORMAT_YUV422) {
            /*
             * YUV422 且未走转换器时, fb->buf 就是传感器的原始 YUV422
             * (Y0 U0 Y1 V1 ...), 每像素 2 字节。
             * 注意: fb->format == YUV422 也可能出现在"走转换器"的模式下,
             * 但那时 fb->buf 里已经是被转成 RGB565 的数据 (格式标记仍是 YUV422)。
             * 我们只在这里导出原始字节做取证, 不显示, 所以两种情况都安全。
             */
            int bytes = (fb->format == PIXFORMAT_GRAYSCALE)
                        ? (fb->width * fb->height)
                        : (fb->width * fb->height * 2);

            if (s_raw_capture_requested && s_raw_snapshot != NULL) {
                memcpy(s_raw_snapshot, fb->buf, (size_t)bytes);
                s_raw_snapshot_seq++;
                s_raw_capture_requested = false;
                ESP_LOGI(TAG, "  已保存原始快照 #%" PRIu32 " (%d 字节)",
                         (uint32_t)s_raw_snapshot_seq, bytes);
            }
            if (s_raw_dump_requested && s_raw_snapshot != NULL) {
                memcpy(s_raw_snapshot, fb->buf, (size_t)bytes);
                s_raw_snapshot_seq++;
                s_raw_capture_requested = false;
                char hdr[64];
                int hn = snprintf(hdr, sizeof(hdr), "RAWDUMP %" PRIu32 " %d\r\n",
                                  (uint32_t)s_raw_snapshot_seq, bytes);
                (void)write(1, hdr, hn);
                (void)write(1, s_raw_snapshot, bytes);
                (void)write(1, "\r\nDONE\r\n", 8);
                s_raw_dump_requested = false;
                ESP_LOGI(TAG, "  已导出原始帧 #%" PRIu32 " (%d 字节)",
                         (uint32_t)s_raw_snapshot_seq, bytes);
            }
            esp_camera_fb_return(fb);
            frames++;
            s_cam_frames++;
            vTaskDelay(1);
            continue;
        }

        if (fb->format != PIXFORMAT_RGB565) {
            ESP_LOGW(TAG, "生产者: 期望 RGB565，实际 %d", (int)fb->format);
            esp_camera_fb_return(fb);
            continue;
        }

        int64_t now = esp_timer_get_time();

        /*
         * 【缓冲稳定性测试】决定性实验。
         *
         * 第一次进来: 记下哈希, 然后【在这里等 250ms】再算一次。
         * 这 250ms 里摄像头会转 5~6 帧。如果第二次哈希不同,
         * 说明这块帧缓冲在我们【还握着它的时候】就被 DMA 覆盖了。
         *
         * (这个测试会让帧率掉到 4fps, 所以只在需要诊断时用命令触发。)
         */
        if (s_hash_test_armed) {
            s_hash_test_a = buf_hash((const uint8_t *)fb->buf,
                                     (size_t)fb->width * fb->height * 2);
            ESP_LOGI(TAG, "HASH: 第一次 = 0x%08X, 等 250ms 再算...", (unsigned)s_hash_test_a);
            vTaskDelay(pdMS_TO_TICKS(250));
            s_hash_test_b = buf_hash((const uint8_t *)fb->buf,
                                     (size_t)fb->width * fb->height * 2);
            s_hash_test_armed = false;
            ESP_LOGI(TAG, "HASH: 第二次 = 0x%08X -> %s",
                     (unsigned)s_hash_test_b,
                     (s_hash_test_a == s_hash_test_b)
                        ? "【相同】帧缓冲稳定, 噪声来自数据本身(传感器/DVP 链路)"
                        : "【不同】帧缓冲被 DMA 覆盖了! 这就是撕裂/花屏的根因");
        }

        /*
         * 噪点行统计 (固件侧第一手数字)。
         *
         * 这是定位花屏的关键测量: 直接看【原始 RGB565】有没有垃圾数据,
         * 不经过 JPEG (JPEG 会把噪点平滑掉, 看不出来)。
         *
         * 采样 30 行, 每行 320 像素 -> 约 19KB, 很便宜。
         */
        if (s_noise_enabled) {
            uint32_t nj = 0, ne = 0, nr = 0;
            measure_noise((const uint8_t *)fb->buf, fb->width, fb->height,
                          &nj, &ne, &nr);
            s_noise_jumps = nj;
            s_noise_energy = ne;
            s_noise_rows = nr;
            s_noise_rows_total += nr;
            s_noise_frames++;
            measure_bytes((const uint8_t *)fb->buf, fb->width, fb->height, &s_bytes);

            /*
             * 【BUG 修复】原来"存原始帧快照"被关在 s_noise_enabled 里,
             * 导致 /raw?capture 请求时如果噪点统计没开, 永远等不到新帧,
             * 返回的是【上一次的旧数据】。
             *
             * 这个 bug 让我们之前的取证分析全部作废:
             * 抓到的是旧帧, 于是误判成"格式不匹配"(其实是随机的位错误)。
             * 现在把抓帧移到 s_noise_enabled 块【外面】, 独立生效。
             */
        }

        /*
         * 【花屏量化】总是算 (开销很小: 只读 30 行 = 19KB)。
         * 这是唯一能客观回答"花屏修好了没有"的指标。
         */
        {
            quality_stat_t q;
            measure_quality((const uint8_t *)fb->buf, fb->width, fb->height, &q);
            s_hi_energy = q.hi_energy;
            s_lo_energy = q.lo_energy;
            s_hi_jumps = q.hi_jumps;
            s_lo_jumps = q.lo_jumps;
            s_bad_rows = q.bad_rows;
            s_qual_frames++;

            /* 【真正的花屏指标】逐通道拆开算, 见上方注释 */
            chan_stat_t cs;
            measure_channel_quality((const uint8_t *)fb->buf, fb->width,
                                    fb->height, &cs);
            s_g2_bad_pct = cs.g2_bad_pct;
            s_g2_energy = cs.g2_energy;
            s_r5_energy = cs.r5_energy;
            s_b5_energy = cs.b5_energy;
        }

        /*
         * ---- 活动检测 (需求文档 §3.5) ----
         *
         * 位置很讲究: 必须在【亮度稳定化】之前, 在【绿色修复】之前。
         *
         * 原因:
         *   - 亮度稳定化会就地修改帧缓冲, 分析器看到的是被改过的画面
         *   - 绿色修复同理
         *   我们要分析的是【摄像头真正拍到的东西】, 不是处理后的结果
         *
         * 内部按 2fps 节流, 所以这里的调用开销可以忽略。
         */
        activity_detect_analyze((const uint8_t *)fb->buf, fb->width, fb->height);

        /*
         * 存一份原始帧快照 (取证用, 独立于噪点统计开关)。
         * 只在明确请求时才存 —— 每帧 153KB 的拷贝会加重 PSRAM 压力。
         */
        if (s_raw_capture_requested && s_raw_snapshot != NULL) {
            memcpy(s_raw_snapshot, fb->buf, RAW_SNAPSHOT_BYTES);
            s_raw_snapshot_seq++;
            s_raw_snapshot_ready = 1;
            s_raw_capture_requested = false;
            ESP_LOGI(TAG, "  已保存原始 RGB565 快照 #%" PRIu32, s_raw_snapshot_seq);
        }

        /*
         * 【串口导出原始帧】—— 不用 HTTP 也能取证。
         *
         * 为什么需要: 设备上的 g2_bad 只是个汇总数字, 想在电脑上
         * 逐位验证到底哪些 bit 被破坏了, 必须把原始字节流拿回来。
         *
         * 协议: 先发一行 ASCII 头 "RAWDUMP <seq> <len>\r\n",
         *       然后原样发 len 字节的二进制, 最后发 "\r\nDONE\r\n"。
         */
        if (s_raw_dump_requested && s_raw_snapshot != NULL) {
            memcpy(s_raw_snapshot, fb->buf, RAW_SNAPSHOT_BYTES);
            s_raw_snapshot_seq++;
            s_raw_capture_requested = false;

            char hdr[64];
            int hn = snprintf(hdr, sizeof(hdr), "RAWDUMP %" PRIu32 " %d\r\n",
                              (uint32_t)s_raw_snapshot_seq, RAW_SNAPSHOT_BYTES);
            /* 用 write() 直出, 避免 ESP_LOG 的格式化和换行干扰二进制流 */
            (void)write(1, hdr, hn);
            (void)write(1, s_raw_snapshot, RAW_SNAPSHOT_BYTES);
            (void)write(1, "\r\nDONE\r\n", 8);
            s_raw_dump_requested = false;
            ESP_LOGI(TAG, "  已导出原始帧 #%" PRIu32 " (%" PRIu32 " 字节)",
                     (uint32_t)s_raw_snapshot_seq, (uint32_t)RAW_SNAPSHOT_BYTES);
        }

        /*
         * 逐行亮度剖面 —— 把"花屏"变成可读的数字。
         * 每 16 行算一个平均亮度。
         *   正常画面: 相邻行的亮度差很小 (画面是连续的自然场景)
         *   花  屏  : 某些行亮度突变, 或者出现 0 / 255 这种极端值
         * 现在改成【低频采样】(每 2 秒一次), 不再每帧都算 ——
         * 每帧算一遍要多读 153600 字节 PSRAM, 代价太大。
         */
        if (now - last_row_dump_us > 2000000) {
            last_row_dump_us = now;
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

        /*
         * ---- 1) 按需拷进 RGB565 槽位 (给 LCD 用) ----
         *
         * 节流条件: 距上次拷贝超过 LCD_PREVIEW_MS, 或者【还没有任何一帧】。
         *
         * 为什么要拷贝而不是直接用 fb->buf:
         *   fb_count=4 且 GRAB_LATEST 时, 摄像头下一帧的 DMA 可能在我们
         *   还没用完这一帧时就覆盖了它 -> 撕裂。拷成副本后 LCD 拿到的
         *   是稳定数据。
         *
         * 但拷贝很贵 (读+写 153KB PSRAM), 而 LCD 只要 5fps,
         * 所以必须按 LCD 的节奏节流, 不能跟着摄像头帧率 (24fps) 跑。
         */
        /*
         * ---- 1) 显示到 LCD ----
         *
         * 【直绘模式】(s_lcd_direct_draw = true, 默认):
         *   不做任何 PSRAM -> PSRAM 拷贝, 直接从摄像头帧缓冲分批画到屏幕。
         *
         * 为什么要改成这样 (这是闪花屏的真正原因, 有实测数据):
         *
         *   旧路径每 200ms 要做一次 153KB 的 PSRAM -> PSRAM memcpy,
         *   实测耗时 4.9ms, 是一口气连续读完写完的。
         *   而就在这 4.9ms 里, 摄像头的 DVP DMA 正在往 PSRAM 里写【下一帧】。
         *   PSRAM 总线是共享的 —— 我们这一口气把总线占满, DVP DMA 抢不到带宽,
         *   下一帧就丢数据。丢掉的那些行在帧缓冲里是【上一次残留或垃圾】,
         *   画到屏幕上就是一条条花带。
         *
         *   esp32-camera 的 README 其实早就警告过:
         *     "Using YUV or RGB puts a lot of strain on the chip because writing
         *      to PSRAM is not particularly fast. The result is that image data
         *      might be missing. This is particularly true if WiFi is enabled."
         *   "image data might be missing" —— 就是我们看到的花屏。
         *
         *   实测证据 (拉 82 帧逐帧分析):
         *     - 每帧有 50~160 行是纯噪声 (相邻像素梯度 > 25)
         *     - 但把 20 帧【平均】起来, 噪声行变成 0/240, 图像完全干净
         *       => 场景本身是稳的, 噪声是叠加在稳定内容上的随机扰动
         *     - 噪声行的位置每帧都不同 (不是固定的几行), 说明是 DMA 丢数据,
         *       不是固定的硬件缺陷
         *
         *   直绘模式怎么解决:
         *     - 彻底去掉那次 153KB 的 PSRAM->PSRAM 拷贝 (省掉一整口气的总线占用)
         *     - 画屏幕本身是【分批】的 (每批 20 行 = 12800 字节):
         *       读一小段 PSRAM -> SPI 发出去 (约 1.7ms) -> 再读下一段
         *       SPI 发送期间 PSRAM 总线是空的, DVP DMA 能插进来
         *     - 画完立刻 esp_camera_fb_return(fb), 摄像头缓冲立刻还回池子
         *
         *   顺带还消除了"撕裂"隐患: 画的时候帧缓冲一直握在手里, 摄像头
         *   不会去覆盖它, 所以画面上下必然来自同一帧。
         *
         * 【旧路径】(s_lcd_direct_draw = false, 保留用于 A/B 对照):
         *   拷进 PSRAM 槽位, 由 lcd_preview_task 读槽位画。
         */
        /*
         * ---- 0) 可选: 软件修复绿色低位 ----
         *
         * 只有在 s_green_fix 打开时才做, 而且只在【真的要显示/编码】时做,
         * 避免每帧都白花 CPU。
         *
         * 注意: repair_green_low 是【就地】修改的, 所以必须放在
         * 显示和编码【之前】, 而且要在统计质量指标【之后】
         * (否则指标就反映不出原始的坏点了)。
         */
        bool need_rgb = false;
        if (last_rgb_copy_us == 0 ||
            (now - last_rgb_copy_us) >= (int64_t)LCD_PREVIEW_MS * 1000) {
            need_rgb = true;
        }

        /*
         * 诊断模式 (lcd1/lcd2) 下屏幕上画的不是摄像头画面,
         * 所以不需要为了显示而处理帧缓冲 —— 否则白花 CPU,
         * 而且就地修改会污染 JPEG 流的原始数据。
         *
         * 但节流时间戳仍然要更新, 否则 need_rgb 每帧都是 true,
         * stabilize_brightness 每帧都跑。
         */
        if (need_rgb && s_lcd_mode != LCD_MODE_CAMERA) {
            last_rgb_copy_us = now;
            need_rgb = false;
        }

        /*
         * ---- 需要 JPEG 吗? ----
         *
         * 三种情况:
         *   1. 有人在拉流 (s_stream_clients > 0) -> 必须编码
         *   2. 推模式没连上 -> 隔一会儿编码一帧存进 JPEG 环形缓存,
         *      这样断网期间还有个现场, 连上后可以补传 (§3.9 要求存 10 帧)
         *   3. 【2026-11-05 新增】推模式连上了 -> 也要限速编码,
         *      否则推任务没有帧可发, 电脑端就只能收到心跳收不到画面。
         *
         * 后两种情况必须【限速】。
         * JPEG 编码很贵 (实测 320x240 @ q65 约 70ms, 占满 CPU),
         * 每帧都编码会把帧率从 8fps 拖到 1fps 以下, 得不偿失。
         * 断网缓存只需要"知道出事那会儿画面长什么样", 2 秒一帧足够了;
         * 实时推送 1 秒一帧也够看 (人对 1fps 的静态画面不敏感,
         * 而对讲/告警走的是事件流, 不依赖画面帧率)。
         */
        bool need_jpg = (s_stream_clients > 0);

        if (!need_jpg) {
            int64_t since = now - last_snapshot_ms;
            bool connected = push_client_is_connected();

            if (connected) {
                /* 已连接 -> 每 1 秒一帧, 实时推给电脑端 */
                if (last_snapshot_ms == 0 || since >= 1000) {
                    need_jpg = true;
                    last_snapshot_ms = now;
                }
            } else {
                /* 断网中 -> 每 2 秒存一帧, 留给补传 */
                if (last_snapshot_ms == 0 || since >= 2000) {
                    need_jpg = true;
                    last_snapshot_ms = now;
                }
            }
        }

        if (s_green_fix && (need_rgb || need_jpg)) {
            repair_green_low((uint8_t *)fb->buf, fb->width, fb->height);
        }

        /*
         * ---- 0b) 亮度稳定 (抵消 GC0308 的 AE 周期震荡) ----
         *
         * 必须在【显示和编码之前】做, 因为它就地修改帧缓冲。
         * 也必须在【质量指标统计之后】做 —— 指标要反映原始数据。
         *
         * 只在真的要显示/编码时才做, 避免每帧白花 CPU。
         */
        if (need_rgb || need_jpg) {
            stabilize_brightness((uint8_t *)fb->buf, fb->width, fb->height);
        }

        /*
         * 【直绘模式】(s_lcd_direct_draw = true, 默认):
         *   不做任何 PSRAM -> PSRAM 拷贝, 直接从摄像头帧缓冲分批画到屏幕。
         *
         * 【重要】必须先检查 s_lcd_mode:
         *   直绘是在【生产者任务】里做的, 而 lcd_preview_task 负责
         *   lcd1 (固定图样) / lcd2 (合成图) 这两个诊断模式。
         *   如果直绘不检查模式, 两个任务会【同时往屏幕画】:
         *     - lcd_preview_task 每秒画一次合成渐变图
         *     - 本任务每 200ms 直绘一次摄像头画面
         *   两者抢 s_lcd_mutex, 屏幕上就是"一闪一闪 + 彩色条纹" ——
         *   而且 lcd1/lcd2 这两个诊断模式其实从来没真正生效过。
         *
         *   所以: 只有在 LCD_MODE_CAMERA 时才允许直绘。
         */
        if (need_rgb && s_lcd_direct_draw && s_lcd_mode == LCD_MODE_CAMERA) {
            /*
             * 屏幕提示期间暂停直绘 (需求文档 §3.8)。
             *
             * 不暂停的话, 提示刚画上去就被下一帧盖掉了。
             * 暂停只发生在帧边界, 所以画面是"静止的旧画面 + 横幅",
             * 不会撕裂。
             */
            if (s_lcd_view_paused) {
                /* 让路给提示层, 不动屏幕 */
            } else {
            int64_t t_draw = esp_timer_get_time();
            esp_err_t derr = lcd_draw_rgb565(fb->buf, fb->width * 2,
                                            0, 0, fb->width, fb->height, true, true);
            s_lcd_draw_us = (uint32_t)(esp_timer_get_time() - t_draw);
            last_rgb_copy_us = t_draw;

            if (derr == ESP_OK) {
                s_lcd_direct_frames++;
            } else {
                ESP_LOGW(TAG, "LCD 直绘失败: %s", esp_err_to_name(derr));
            }
            }
        } else if (need_rgb && s_lcd_mode == LCD_MODE_CAMERA) {
            int rgb_idx = -1;
            if (slot_acquire(s_rgb_slots, &rgb_idx)) {
                int64_t t_copy = esp_timer_get_time();
                memcpy(s_rgb_slots[rgb_idx].buf, fb->buf, RGB565_SLOT_BYTES);
                s_rgb_slots[rgb_idx].len = RGB565_SLOT_BYTES;
                s_rgb_copy_us = (uint32_t)(esp_timer_get_time() - t_copy);
                last_rgb_copy_us = t_copy;

                s_clean_frames++;
                slot_publish(s_rgb_slots, rgb_idx);
            } else {
                /* 没有空槽 (LCD 还没取走上一帧) -> 丢弃这一帧, 等下一帧 */
                s_torn_frames++;
            }
        }

        /*
         * ---- 2) 按需编码 JPEG (给视频流用) ----
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
         *
         * 编码直接吃 fb->buf (不再经过 RGB 槽位):
         *   JPEG 编码输出到独立的 JPEG 槽位, 和 RGB 槽位互不干扰,
         *   没必要再从 PSRAM 拷一份 153KB 出来。
         */
        if (need_jpg) {
            int jpg_idx = -1;
            if (slot_acquire(s_jpg_slots, &jpg_idx)) {
                int64_t t_enc = esp_timer_get_time();
                size_t jlen = encode_jpeg_to_slot(fb->buf,
                                                  fb->width, fb->height,
                                                  s_jpg_slots[jpg_idx].buf);
                s_jpg_encode_us = (uint32_t)(esp_timer_get_time() - t_enc);

                if (jlen > 0) {
                    s_jpg_slots[jpg_idx].len = jlen;
                    s_jpg_bytes = (uint32_t)jlen;
                    s_jpg_frames++;
                    slot_publish(s_jpg_slots, jpg_idx);

                    /*
                     * §3.9 断网缓存 + 实时推送: 把这帧交给推模式客户端。
                     *
                     * 【2026-11-05】推任务现在会【持续】从队列里取帧发出,
                     * 所以这里每帧都要交进去 —— 不只是断网时。
                     *
                     * 之前只在 s_stream_clients == 0 时才存, 逻辑是
                     * "没人拉流时才缓存, 留着断网后补传"。
                     * 但推模式连着的时候这帧是要【实时】发给电脑端的,
                     * 不交进去电脑端就只能收到心跳收不到画面。
                     *
                     * 有 HTTP 拉流客户端时也照样交 —— 两条链路是并行的,
                     * 拉流走 httpd, 推流走 TCP push, 互不冲突。
                     */
                    push_client_push_jpeg(s_jpg_slots[jpg_idx].buf, jlen);
                } else {
                    slot_release(s_jpg_slots, jpg_idx);
                }
            }
        } else {
            s_jpg_encode_us = 0;   /* 未编码 */
        }

        esp_camera_fb_return(fb);

        frames++;
        s_cam_frames++;
        if (frames == 1) {
            t0 = esp_timer_get_time();
        } else if (frames % 50 == 0) {
            int64_t dt_us = esp_timer_get_time() - t0;
            ESP_LOGI(TAG, "PROD: %d frames (%.1f fps) | get %" PRIu32 "us copy %" PRIu32 "us jpeg %" PRIu32 "us | jpg %" PRIu32 " bytes | DROP %" PRIu32,
                     frames, (frames - 1) * 1000000.0 / (double)dt_us,
                     s_cam_get_us, s_rgb_copy_us,
                     s_jpg_encode_us, s_jpg_bytes,
                     s_torn_frames);
            if (s_lcd_direct_draw) {
                ESP_LOGI(TAG, "  LCD 直绘: %" PRIu32 " 帧, 最近一次 %" PRIu32 "us",
                         s_lcd_direct_frames, s_lcd_draw_us);
            }
            if (s_noise_enabled) {
                ESP_LOGI(TAG, "  噪点: rows %" PRIu32 "/%d  jumps %" PRIu32 "  energy %" PRIu32
                         "  | hi(energy %" PRIu32 " jumps %" PRIu32 ")  lo(energy %" PRIu32 " jumps %" PRIu32 ")",
                         s_noise_rows, NOISE_PROBE_ROWS,
                         s_noise_jumps, s_noise_energy,
                         s_bytes.hi_energy, s_bytes.hi_jumps,
                         s_bytes.lo_energy, s_bytes.lo_jumps);
            }
        }

        /*
         * 周期性让出 CPU (喂看门狗)。
         *
         * 这个任务优先级 6 (最高), 一旦开始跑就会一直霸着 CPU,
         * 因为 esp_camera_fb_get() 是【阻塞】的所以平时还好。
         * 但一旦有人拉视频流, JPEG 编码 + PSRAM 拷贝连在一起,
         * 就会出现 "IDLE0 跑不到 -> 看门狗超时 -> 板子重启"。
         *
         * 这里每 16 帧阻塞 1 tick, 相当于 24fps 下每 ~660ms 让一次,
         * 保证 IDLE0 有机会喂狗。
         *
         * 注意: 用 tick 数而不是 pdMS_TO_TICKS(1) —— 后者在 100Hz 下是 0。
         */
        if ((frames % 16) == 0) {
            vTaskDelay(APP_DELAY_TICKS);
        }

        /*
         * §3.9 看门狗: 生产者是唯一碰摄像头的任务。
         *
         * 它卡住 = 画面停住 = 子女以为老人出事了却看不到任何变化。
         * 这个任务必须喂狗, 卡住就该复位。
         */
        reliability_feed_watchdog();
    }
}

/* ---------------- 屏幕提示的显示所有权 (需求文档 §3.8) ---------------- */

/*
 * 暂停摄像头直绘 —— 让屏幕提示层独占屏幕。
 *
 * lcd_prompt.c 在画横幅前调这个, 画完/到期后调 lcd_view_resume()。
 *
 * 【为什么用标志位而不是锁】
 *   提示任务要画横幅, 生产者要画画面, 两者都要写 LCD。
 *   如果用互斥锁:
 *     - 提示任务要等生产者画完当前帧才能拿锁 (最长 40ms), 能接受
 *     - 但提示【显示期间】(最长 10 秒) 生产者如果一直持锁, 提示就画不了
 *   所以拆成两半:
 *     - 标志位告诉生产者"别画了"
 *     - 生产者在【帧边界】检查标志, 所以画面不会撕裂
 *
 * 【时序】
 *   1. 提示任务置标志
 *   2. 等 60ms —— 让生产者完成当前这一帧 (否则可能出现"半帧 + 横幅")
 *   3. 提示任务画横幅
 *   4. 到期 -> 清标志 -> 生产者下一帧自然覆盖回来
 */
void lcd_view_pause(void)
{
    if (s_lcd_view_paused) {
        return;
    }
    s_lcd_view_paused = true;
    s_lcd_pause_count++;

    /*
     * 等生产者让出屏幕。
     *
     * 60ms 是按"一帧直绘最长 ~40ms + 一点余量"估的。
     * 等不到也没关系 —— 最多是横幅和画面重叠一小会儿,
     * 不会崩, 只是可能有一帧的撕裂感。
     */
    vTaskDelay(pdMS_TO_TICKS(60));
}

/* 恢复摄像头直绘 */
void lcd_view_resume(void)
{
    if (!s_lcd_view_paused) {
        return;
    }
    s_lcd_view_paused = false;
}

/* 当前是否处于"提示独占屏幕"状态 (调试用) */
bool lcd_view_is_paused(void)
{
    return s_lcd_view_paused;
}

/* ---------------- LCD 预览任务 (消费者) ---------------- */

/*
 * 只从 RGB565 槽位取现成的帧, 不碰摄像头。
 * 降频到 ~2 fps —— LCD 预览是给老人自己看的"我在线"指示,
 * 不需要高帧率; 把 CPU 全留给远程视频流。
 *
 * 注意: lcd1/lcd2 两个诊断模式也在这个任务里实现,
 * 枚举 lcd_mode_t 的定义在文件前面 (s_lcd_direct_draw 旁边),
 * 因为 camera_producer_task 也要用它来决定要不要让路。
 */
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

        /*
         * 直绘模式 (s_lcd_direct_draw = true):
         *   显示由 camera_producer_task 负责 (它手里一直握着帧缓冲,
         *   画的时候不会被摄像头覆盖, 所以不会撕裂)。
         *   这个任务就空转等待, 不要重复画。
         */
        if (s_lcd_direct_draw) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        int idx = -1;
        size_t len = 0;

        if (slot_take_ready(s_rgb_slots, &idx, &len)) {
            /* 正常整屏显示 (真实产品路径) */
            lcd_draw_rgb565(s_rgb_slots[idx].buf, 320 * 2, 0, 0, 320, 240, true, true);
            slot_release(s_rgb_slots, idx);
        } else {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        vTaskDelay(pdMS_TO_TICKS(LCD_PREVIEW_MS));
    }
}

/* ---------------- JPEG 编码 (流式, 零额外拷贝, 带 CPU 让出点) ---------------- */

/*
 * 用 jpeg_encode_rgb565_yield() 而不是 frame2jpg:
 *   frame2jpg     每帧 malloc 128KB -> 编码 -> memcpy 到缓冲 -> free
 *                 在 PSRAM 上反复大块分配/释放, 既慢又引起碎片
 *   fmt2jpg_cb    编码器直接把 512 字节一块的数据推给回调, 我们顺手写进
 *                 预分配的槽位, 没有额外拷贝, 也没有每帧 malloc
 *                 但【不让出 CPU】 -> 看门狗超时 -> 板子重启 (见下)
 *
 * jpeg_encode_rgb565_yield 是我们自己写的第三种:
 *   编码结果和 fmt2jpg_cb 一模一样 (同样 jpge 参数, 同样的行转换),
 *   区别是每 16 行主动让出一次 CPU。
 *
 * 支持 RGB565 输入, 内部逐行展开成 RGB888 (用行缓冲区, 不是整帧中间缓冲),
 * 然后 jpge 再转 YCC。所以直接喂 RGB565, 不要自己先展开成整帧 RGB888
 * —— 那会多占 230KB。
 */

/*
 * 把一帧 RGB565 编码成 JPEG, 写进 dst。
 * 返回写入的字节数 (0 = 失败)。
 *
 * 注意: 摄像头帧缓冲是大端 (高位字节在前), jpge 默认 rgb565_big_endian = true,
 * 与我们的帧缓冲格式一致, 不用做字节交换。
 *
 * ---------------------------------------------------------------------------
 * 为什么不用 fmt2jpg_cb:
 *   fmt2jpg_cb() 是 esp32-camera 的黑盒, 它在 convert_image() 里一口气把
 *   240 行全部编码完, 中间【不让出 CPU】。实测一次要 69ms。
 *
 *   后果 (串口日志实锤):
 *     E task_wdt: Task watchdog got triggered.
 *     E task_wdt:  - IDLE0 (CPU 0)
 *     E task_wdt: CPU 0: cam_producer
 *   -> core 0 的空闲任务喂不了看门狗 -> 板子反复重启
 *   -> 同时 prio 2 的 LCD 任务被饿死 -> 屏幕卡住 -> 看起来在闪花屏
 *
 * 所以改用 jpeg_encode_rgb565_yield(): 同样的编码结果,
 * 但每 16 行主动让出一次 CPU。
 */
static void jpeg_encode_yield_cb(void *arg)
{
    (void)arg;
    /*
     * 这里必须【阻塞】, 不能只 taskYIELD()。
     *
     * taskYIELD() 只是把 CPU 让给同优先级的就绪任务, 自己马上又跑起来。
     * 而看门狗要复位的是 IDLE0 (优先级最低), 只有当编码任务真正
     * 阻塞时, IDLE0 才有机会跑起来喂狗。
     *
     * 陷阱: 不能写 pdMS_TO_TICKS(1)! tick 率是 100Hz, 1ms 不足一个 tick,
     *       pdMS_TO_TICKS(1) 会算成 0, vTaskDelay(0) 退化成 taskYIELD(),
     *       让出点等于没加 (编码耗时还是 69ms, 看门狗照样超时)。
     *       必须直接用 tick 数: vTaskDelay(1) = 10ms。
     */
    vTaskDelay(APP_DELAY_TICKS);
}

static size_t encode_jpeg_to_slot(const uint8_t *rgb565, int w, int h, uint8_t *dst)
{
    size_t written = 0;

    if (!jpeg_encode_rgb565_yield(rgb565, w, h, JPEG_QUALITY,
                                  dst, JPEG_SLOT_BYTES, &written,
                                  jpeg_encode_yield_cb, NULL, JPEG_YIELD_EVERY_ROWS)) {
        return 0;
    }
    return written;
}


static esp_err_t root_handler(httpd_req_t *req)
{
    /*
     * 需求文档 3.4 节要求的最小页面: 一个 <img> + 一个弹窗。
     * 这里额外加了两点, 都是为了【验收时能一眼看懂】:
     *   - 提示框做成醒目的卡片 (老人/评审一眼就能看到)
     *   - 加一个连接状态灯, 能区分"没事件"和"设备没连上"
     *
     * 【踩过的坑】这段 JS 的花括号必须严格配平。
     *   曾经多写了一个 '}', 导致整段脚本抛 SyntaxError,
     *   页面看起来正常但轮询根本没启动 —— 按钮事件永远弹不出来。
     *   教训: 内联 JS 一定要在浏览器控制台里确认没有红色报错。
     */
    const char html[] =
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>远程看护盒子</title>"
        "<style>body{font-family:sans-serif;margin:20px;background:#fafafa}"
        "img{border:2px solid #333;max-width:100%;background:#000}"
        "#alert{display:none;background:#ffe0e0;border:2px solid #d00;padding:16px;"
        "margin-top:16px;font-size:20px;font-weight:bold;border-radius:8px}"
        "#status{font-size:14px;color:#666;margin:8px 0}"
        ".dot{display:inline-block;width:10px;height:10px;border-radius:50%;"
        "background:#c00;margin-right:6px;vertical-align:middle}"
        ".dot.on{background:#0a0}"
        "</style></head><body>"
        "<h2>远程看护盒子（可行性验证）</h2>"
        "<div id=\"status\"><span class=\"dot\" id=\"dot\"></span>"
        "<span id=\"statusText\">正在连接设备...</span></div>"
        "<img id=\"cam\" src=\"/stream\" width=\"320\" height=\"240\">"
        "<div id=\"alert\"></div>"
        "<script>"
        "var dot=document.getElementById('dot');"
        "var st=document.getElementById('statusText');"
        "var alertBox=document.getElementById('alert');"
        "function poll(){"
        "fetch('/event').then(function(r){return r.json();}).then(function(d){"
        "dot.className='dot on';"
        "st.textContent='设备在线';"
        "if(d.type==='button'){"
        "alertBox.textContent=d.msg;"
        "alertBox.style.display='block';"
        "fetch('/notify',{method:'POST',body:d.msg});"
        "setTimeout(function(){alertBox.style.display='none';},8000);"
        "}"
        "}).catch(function(e){"
        "dot.className='dot';"
        "st.textContent='连接中断,重试中...';"
        "});"
        "}"
        "setInterval(poll,500);"
        "poll();"
        "</script></body></html>";

    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send(req, html, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/* MJPEG 流的分块发送器 */
/*
 * 【2026-11-07 新增】探测 /stream 客户端是否已断开 (零副作用)。
 *
 * 【为什么不能用 httpd_resp_send_chunk(req, NULL, 0)】
 *   IDF 文档: "When you are finished sending all your chunks, you must
 *   call this function with buf_len as 0."
 *   也就是说 buf_len=0 的语义是【结束 chunked 响应】, 会发 "0\r\n\r\n"
 *   终止符, 不是"发 0 字节探测"。
 *
 *   用它做探测的后果 (实测): 消费者只要超过 100ms 没等到新帧,
 *   整个 multipart 流就被提前终止 -> 客户端停止读取并断开 -> 画面卡顿。
 *
 * 【怎么做到零副作用】
 *   用 MSG_PEEK 读 socket:
 *     - 返回 0    -> 对方发了 FIN (半关闭), 客户端已断开
 *     - 返回 <0   -> 出错 (含 RST), 客户端已断开
 *     - 返回 >0   -> 客户端发来了数据, peek 出来丢掉即可 (不影响)
 *   全程不往线上写任何字节, 不会破坏 chunked 协议。
 */
static bool stream_client_alive(httpd_req_t *req)
{
    int fd = httpd_req_to_sockfd(req);
    if (fd < 0) {
        return false;
    }

    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(fd, &rfds);
    struct timeval tv = { .tv_sec = 0, .tv_usec = 0 };

    /* 只等 0 秒: 有数据/异常立即返回, 没有就当"还活着" */
    int r = select(fd + 1, &rfds, NULL, NULL, &tv);
    if (r <= 0) {
        return true;   /* 没动静 -> 还活着 */
    }

    char tmp[64];
    int n = recv(fd, tmp, sizeof(tmp), MSG_PEEK);
    if (n > 0) {
        /* 客户端发来了数据, peek 掉不算消费, 协议上无害 */
        return true;
    }
    if (n == 0) {
        return false;  /* FIN: 对方已关闭写方向 */
    }
    return false;      /* 出错 (含 RST) */
}

static esp_err_t stream_handler(httpd_req_t *req)
{
    /*
     * 【2026-11-06】限制并发推流客户端数。
     *
     * ESP-IDF 的 http_server:
     *   - max_open_sockets 硬上限 7 (内部占 3, 客户端最多 4)
     *   - 每个活跃请求占用 1 个 socket + 1 个 httpd worker 线程
     * /stream 是长连接, 一个客户端就长期占住这两个资源。
     *
     * 实测事故: 控制台代理 + 几个浏览器标签页一起连, 把 4 个槽位占满,
     * /health /activity /event 这些短请求全部排队 -> 表现为"板子 HTTP 挂了",
     * 但串口里相机其实还在正常跑 (5.8 fps), 板子本身没死。
     *
     * 所以这里必须【主动限流】: 超过上限直接回 503, 让客户端稍后重试,
     * 把 socket 槽位留给短请求。短请求才是"看护"场景的刚需 ——
     * 画面卡可以接受, 收告警绝不能卡。
     */
    if (s_stream_clients >= STREAM_MAX_CLIENTS) {
        ESP_LOGW(TAG, "推流客户端已满 (%d)，拒绝新连接", STREAM_MAX_CLIENTS);
        httpd_resp_set_status(req, "503 Service Unavailable");
        httpd_resp_set_type(req, "text/plain; charset=utf-8");
        httpd_resp_sendstr(req, "too many stream clients");
        return ESP_FAIL;
    }

    /* 长连接总配额 (视频 + 音频 共享) —— 留 1 个槽位给短请求 */
    if (!long_conn_acquire()) {
        ESP_LOGW(TAG, "长连接配额已满 (%d)，拒绝推流", LONG_CONN_MAX);
        httpd_resp_set_status(req, "503 Service Unavailable");
        httpd_resp_set_type(req, "text/plain; charset=utf-8");
        httpd_resp_sendstr(req, "too many long connections");
        return ESP_FAIL;
    }

    s_stream_clients++;
    ESP_LOGI(TAG, "画面客户端接入，当前 %d 个 (长连接 %d)",
             s_stream_clients, s_long_conns);

    /*
     * 【踩过的坑】不能用 httpd_resp_send() 来发 multipart 响应头!
     *
     *   httpd_resp_send() 内部会【自己拼一整套 HTTP 响应头】:
     *     HTTP/1.1 200 OK
     *     Content-Type: text/html      <-- 默认类型
     *     Content-Length: 148
     *   然后才把你传进去的字符串当成【正文】发出去。
     *
     *   结果就是服务端一口气发了两个 HTTP 响应:
     *     第一个: 200 OK, text/html, Content-Length: 148
     *              正文是 "Content-Type: multipart/..." 这段字符串
     *     第二个: 200 OK, chunked, 真正的 JPEG 数据
     *
     *   浏览器只认第一个响应, 把它当成"一个 148 字节的 text/html",
     *   渲染出来就是一片空白 —— <img> 的 naturalWidth = 0。
     *
     *   正确做法:
     *     - httpd_resp_set_type()  设置 Content-Type (只改头, 不发数据)
     *     - httpd_resp_set_hdr()   设置额外的响应头
     *     - httpd_resp_send_chunk() 用分块传输发数据 (自动带
     *       Transfer-Encoding: chunked, 浏览器能正确解析 multipart 流)
     */
    httpd_resp_set_type(req, "multipart/x-mixed-replace; boundary=frame");
    httpd_resp_set_hdr(req, "Cache-Control", "no-cache, no-store, must-revalidate");
    httpd_resp_set_hdr(req, "Pragma", "no-cache");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

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

            /*
             * 【2026-11-06】等帧期间主动检查客户端是否已断开。
             *
             * 之前只在"发送失败"时才退出循环。但浏览器关掉标签页时
             * 是【半关闭】(FIN_WAIT_2), 板子这边如果一直等帧不写数据,
             * 就永远收不到 RST, socket 一直占着。
             *
             * 现场证据: 板子到 8080 的连接里堆了 3 个 FIN_WAIT_2,
             * 数据面 /health 直接超时 —— 槽位被这些"已死但没释放"的
             * 连接占着。浏览器每次刷新页面就多一个。
             *
             * 解决: 每隔一小段时间用 send 探测一次 (发 0 字节),
             * TCP 会返回错误, 我们就能立刻感知断开并释放 socket。
             */
            static int idle_ticks = 0;
            if (++idle_ticks >= 25) {   /* 每 20ms 一次 -> 500ms 探一次 */
                idle_ticks = 0;
                if (!stream_client_alive(req)) {
                    ESP_LOGI(TAG, "画面客户端已断开 (socket 探测发现)");
                    break;
                }
            }
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

    s_stream_clients--;
    long_conn_release();
    ESP_LOGI(TAG, "画面客户端断开，剩余 %d 个 (长连接 %d)",
             s_stream_clients, s_long_conns);
    return ESP_OK;
}

/*
 * 把一段文本安全地写进 JSON 字符串字面量。
 *
 * 为什么需要:
 *   /event 直接把文案拼进 JSON。如果文案里有 " 或 \ 或控制字符,
 *   拼出来的就是非法 JSON, 网页的 JSON.parse 会抛异常,
 *   整条链路静默失效 (页面不报错, 只是永远不弹窗)。
 *   中文本身没问题 (UTF-8 多字节直接透传即可), 要转义的是这些字符。
 *
 * 返回写入的字节数 (不含结尾的 '\0')。
 */
static int json_escape(char *dst, size_t dst_size, const char *src)
{
    size_t o = 0;
    for (size_t i = 0; src[i] != '\0' && o + 7 < dst_size; i++) {
        unsigned char c = (unsigned char)src[i];
        switch (c) {
        case '"':  dst[o++] = '\\'; dst[o++] = '"';  break;
        case '\\': dst[o++] = '\\'; dst[o++] = '\\'; break;
        case '\n': dst[o++] = '\\'; dst[o++] = 'n';  break;
        case '\r': dst[o++] = '\\'; dst[o++] = 'r';  break;
        case '\t': dst[o++] = '\\'; dst[o++] = 't';  break;
        default:
            if (c < 0x20) {
                /* 其它控制字符用 \u00XX */
                o += snprintf(dst + o, dst_size - o, "\\u%04x", c);
            } else {
                dst[o++] = (char)c;   /* 中文等 UTF-8 多字节原样透传 */
            }
            break;
        }
    }
    dst[o] = '\0';
    return (int)o;
}

static esp_err_t event_handler(httpd_req_t *req)
{
    char resp[384];
    char msg_esc[256];

    button_event_t ev;
    if (xQueueReceive(s_event_queue, &ev, 0) == pdTRUE) {
        json_escape(msg_esc, sizeof(msg_esc), ev.message);
        snprintf(resp, sizeof(resp),
                 "{\"type\":\"button\",\"msg\":\"%s\",\"seq\":%u,\"ts\":%llu}",
                 msg_esc, (unsigned int)ev.seq, (unsigned long long)ev.ts_ms);
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

/*
 * POST /cmd —— 家人指令通道 (需求文档 §3.8)
 *
 * ============================ 为什么用 HTTP 先顶着 ============================
 *
 * §3.8 定义的下行指令是二进制帧 type=0x11:
 *
 *   | 家人正在看画面 | 收到 0x11 指令 | 屏幕显示「家人正在看画面」, 5s 后熄灭 |
 *   | 家人正在呼叫   | 收到 0x11 指令 | 屏幕显示「家人正在呼叫您」, 伴随提示音 |
 *   | 干预内容播放   | 收到 0x11 指令 | 屏幕显示「该活动一下」等文案 + 播放音频 |
 *
 * 但 M3 的 WSS 上云还没做, 现在没有可靠的二进制下行通道。
 * 所以本期先用 HTTP POST 模拟, 语义和 0x11 完全一致,
 * M3 上云后把这里的内容映射到 0x11 就行, 业务代码不用改。
 *
 * ============================ 请求格式 ============================
 *
 *   {"cmd":"watching"}                       -> 「家人正在看画面」
 *   {"cmd":"calling"}                        -> 「家人正在呼叫您」+ 提示音
 *   {"cmd":"content","text":"该活动一下"}      -> 自定义文案
 *
 * 也支持带时长的完整形式:
 *   {"cmd":"watching","duration_ms":3000}
 *
 * ============================ 为什么"看画面"要能被设备知道 ============================
 *
 * 这是 §3.8 的核心 —— 防惊吓机制。
 * 子女在小程序里点"看画面", 老人这边的屏幕必须亮出"家人正在看画面",
 * 否则老人发现自己在被偷拍, 会恐慌, 会砸设备, 会拔电源。
 *
 * 所以这个端点不是"彩蛋", 是产品能不能用的前提。
 */
static esp_err_t cmd_handler(httpd_req_t *req)
{
    char buf[256];
    int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (len <= 0) {
        len = 0;
    }
    buf[len] = '\0';

    /* 极简 JSON 解析: 找 "cmd" 和 "text" 字段 */
    char cmd[32] = {0};
    char text[128] = {0};
    uint32_t duration_ms = 0;

    const char *p = strstr(buf, "\"cmd\"");
    if (p) {
        p = strchr(p, ':');
        if (p) {
            p = strchr(p, '"');
            if (p) {
                p++;
                size_t i = 0;
                while (*p && *p != '"' && i < sizeof(cmd) - 1) {
                    cmd[i++] = *p++;
                }
                cmd[i] = '\0';
            }
        }
    }

    p = strstr(buf, "\"text\"");
    if (p) {
        p = strchr(p, ':');
        if (p) {
            p = strchr(p, '"');
            if (p) {
                p++;
                size_t i = 0;
                while (*p && *p != '"' && i < sizeof(text) - 1) {
                    text[i++] = *p++;
                }
                text[i] = '\0';
            }
        }
    }

    p = strstr(buf, "\"duration_ms\"");
    if (p) {
        p = strchr(p, ':');
        if (p) {
            duration_ms = (uint32_t)atoi(p + 1);
        }
    }

    ESP_LOGI(TAG, "收到家人指令: cmd=%s text=%s duration=%" PRIu32,
             cmd[0] ? cmd : "(空)", text[0] ? text : "(空)", (uint32_t)duration_ms);

    if (strcmp(cmd, "watching") == 0) {
        /* 家人正在看画面 —— 5s 后熄灭 */
        lcd_prompt_show(PROMPT_WATCHING, NULL, duration_ms);
        ESP_LOGI(TAG, "屏幕提示: 家人正在看画面");
    } else if (strcmp(cmd, "calling") == 0) {
        /* 家人正在呼叫 —— 伴随提示音 */
        lcd_prompt_show(PROMPT_CALLING, NULL, duration_ms);
        /*
         * 提示音: 一次性「叮咚」, 不是循环测试音。
         *
         * 需求文档 §3.8 明确要求"伴随提示音"。用循环测试音的话会一直响,
         * 老人听不出这是一次提示, 而且吓人的程度远超"防惊吓"的初衷。
         */
        audio_board_play_prompt_beep();
        ESP_LOGI(TAG, "屏幕提示: 家人正在呼叫您 (伴随提示音)");
    } else if (strcmp(cmd, "content") == 0) {
        /* 干预内容播放 —— 自定义文案 */
        lcd_prompt_show(PROMPT_CONTENT, text[0] ? text : NULL, duration_ms);
        ESP_LOGI(TAG, "屏幕提示: 干预内容「%s」", text[0] ? text : "该活动一下");
    } else {
        httpd_resp_set_type(req, "application/json; charset=utf-8");
        httpd_resp_send(req,
                        "{\"ok\":false,\"error\":\"unknown cmd\","
                        "\"supported\":[\"watching\",\"calling\",\"content\"]}",
                        HTTPD_RESP_USE_STRLEN);
        return ESP_OK;
    }

    httpd_resp_set_type(req, "application/json; charset=utf-8");
    httpd_resp_send(req, "{\"ok\":true}", HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/*
 * /raw —— 导出【原始 RGB565】帧 (取证用)。
 *
 * 为什么要这个端点:
 *   /stream 给的是 JPEG, 而 JPEG 会把噪点平滑掉, 根本看不出画面脏在哪。
 *   只有拿到未编码的原始 RGB565, 才能在电脑上逐字节分析:
 *   哪些字节被污染、是整行错位还是逐像素跳变、是不是半个像素的错位。
 *
 * 用法:
 *   GET /raw          -> 返回最近抓的一帧原始 RGB565 (153600 字节, 二进制)
 *   GET /raw?capture  -> 先请求抓一帧, 再返回
 */
static esp_err_t raw_handler(httpd_req_t *req)
{
    if (s_raw_snapshot == NULL) {
        httpd_resp_sendstr(req, "raw snapshot buffer not allocated");
        return ESP_OK;
    }

    /* 带了 capture 参数 = 先等一帧新的 */
    char query[64] = {0};
    if (httpd_req_get_url_query_len(req) > 0 &&
        httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK) {
        if (strstr(query, "capture") != NULL) {
            /*
             * 【BUG 修复】原来只看 s_raw_snapshot_ready 这个标志,
             * 而它可能是上一轮留下的 1, 于是循环立刻退出,
             * 返回的是【旧帧】。这就是之前取证全部作废的原因。
             *
             * 正确做法: 记住当前序号, 等到序号【变大】才算抓到了新帧。
             */
            uint32_t seq_before = s_raw_snapshot_seq;
            s_raw_capture_requested = true;
            /* 等生产者抓帧 (最多 2 秒) */
            for (int i = 0; i < 200; i++) {
                if (s_raw_snapshot_seq != seq_before) {
                    break;
                }
                vTaskDelay(pdMS_TO_TICKS(10));
            }
            s_raw_snapshot_ready = 0;
            ESP_LOGI(TAG, "RAW: 序号 %" PRIu32 " -> %" PRIu32,
                     (uint32_t)seq_before, (uint32_t)s_raw_snapshot_seq);
        }
    }

    httpd_resp_set_type(req, "application/octet-stream");
    httpd_resp_set_hdr(req, "Content-Disposition", "attachment; filename=raw.rgb565");
    httpd_resp_send(req, (const char *)s_raw_snapshot, RAW_SNAPSHOT_BYTES);
    return ESP_OK;
}

static esp_err_t health_handler(httpd_req_t *req)
{
    esp_netif_ip_info_t ip;
    esp_netif_get_ip_info(s_netif, &ip);

    /*
     * §3.9 推模式统计 + 可靠性状态。
     *
     * 放进 /health 是因为这是唯一不需要额外工具就能看到的地方 ——
     * 排查"设备为什么没推事件"时, 先看这里。
     */
    push_stats_t ps;
    push_client_get_stats(&ps);

    heartbeat_t hb;
    reliability_get_heartbeat(&hb);

    /*
     * 电源 / 电池状态。
     *
     * 为什么要放进 /health:
     *   "拔掉 Type-C 就没画面" 的根因是板子彻底断电 (AW9523B 一死,
     *   所有外设电源轨全断)。要判断设备现在到底是 USB 供电还是电池供电,
     *   以及电量还剩多少, 这是唯一不需要额外工具就能看到的地方。
     *
     * power_source 取值: "typec" / "battery" / "unknown"
     */
    power_status_t pst = power_board_get_status();
    const char *psrc = "unknown";
    if (pst.source == POWER_SOURCE_TYPEC)        psrc = "typec";
    else if (pst.source == POWER_SOURCE_BATTERY)  psrc = "battery";

    /*
     * AI 对话状态。
     *
     * 为什么要放进 /health:
     *   "老人跟盒子说话, 盒子有没有听清? 大模型回了什么? 服务器连上了吗?"
     *   这三个问题是排查语音对话的唯一入口 —— 而且不需要人对着板子说话,
     *   看日志就行。
     *
     * ai_last_user / ai_last_reply 是中文, 直接放进 JSON 会有转义问题,
     * 所以这里做了简单清洗 (去掉引号/反斜杠/控制字符)。
     */
    ai_client_stats_t ais;
    ai_client_get_stats(&ais);
    {
        /* 清洗: JSON 字符串里不能有裸引号/反斜杠 */
        char *dst = ais.last_user;
        size_t cap = sizeof(ais.last_user);
        for (size_t i = 0; dst[i] && i + 1 < cap; i++) {
            char c = dst[i];
            if (c == '"' || c == '\\' || (unsigned char)c < 0x20) {
                dst[i] = ' ';
            }
        }
        dst = ais.last_reply;
        cap = sizeof(ais.last_reply);
        for (size_t i = 0; dst[i] && i + 1 < cap; i++) {
            char c = dst[i];
            if (c == '"' || c == '\\' || (unsigned char)c < 0x20) {
                dst[i] = ' ';
            }
        }
    }

    char resp[1536];
    snprintf(resp, sizeof(resp),
             "{\"ip\":\"" IPSTR "\",\"uptime\":%llu,\"frames\":%" PRIu32 ",\"clients\":%d,\"long_conns\":%d,"
             "\"psram\":%" PRIu32 ","
             "\"cam_frames\":%" PRIu32 ",\"jpg_frames\":%" PRIu32 ",\"jpg_bytes\":%" PRIu32 ","
             "\"cam_get_us\":%" PRIu32 ",\"rgb_copy_us\":%" PRIu32 ",\"jpg_encode_us\":%" PRIu32 ","
             "\"torn_frames\":%" PRIu32 ","
             "\"lcd_direct\":%s,\"lcd_draw_us\":%" PRIu32 ",\"lcd_frames\":%" PRIu32 ","
             "\"noise_rows\":%" PRIu32 ",\"noise_avg\":%" PRIu32 ","
             "\"hi_energy\":%" PRIu32 ",\"lo_energy\":%" PRIu32 ","
             "\"hi_jumps\":%" PRIu32 ",\"lo_jumps\":%" PRIu32 ",\"bad_rows\":%" PRIu32 ","
             "\"jpeg_quality\":%d,\"max_fps\":%" PRIu32 ","
             "\"audio_ready\":%s,\"audio_rate\":%d,"
             "\"time_synced\":%s,\"epoch_ms\":%lld,"
             "\"rssi\":%d,\"free_heap\":%u,\"free_psram\":%u,"
             "\"mode\":\"%s\",\"camera_ok\":%s,"
             "\"power_source\":\"%s\",\"charging\":%s,"
             "\"battery_pct\":%d,\"battery_mv\":%d,\"battery_valid\":%s,"
             "\"push_connected\":%s,\"push_sent\":%u,\"push_jpegs\":%u,"
             "\"push_replayed\":%u,\"push_dropped\":%u,\"push_queued\":%u,"
             "\"push_fails\":%u,\"push_backoff_ms\":%u,"
             "\"ai_state\":\"%s\",\"ai_connected\":%s,\"ai_turns\":%u,\"ai_replies\":%u,"
             "\"ai_errors\":%u,\"ai_last_user\":\"%s\",\"ai_last_reply\":\"%s\","
             "\"wake_ready\":%s,\"wake_hits\":%u,\"wake_prob\":%.3f,"
             "\"wake_name\":\"%s\",\"voice_mode\":\"%s\"}",
             IP2STR(&ip.ip),
             (unsigned long long)(esp_timer_get_time() / 1000),
             (uint32_t)s_frame_count,
             s_stream_clients,
             s_long_conns,
             (uint32_t)esp_psram_get_size(),
             (uint32_t)s_cam_frames,
             (uint32_t)s_jpg_frames,
             (uint32_t)s_jpg_bytes,
             (uint32_t)s_cam_get_us,
             (uint32_t)s_rgb_copy_us,
             (uint32_t)s_jpg_encode_us,
             (uint32_t)s_torn_frames,
             s_lcd_direct_draw ? "true" : "false",
             s_lcd_draw_us,
             s_lcd_direct_frames,
             s_noise_rows,
             s_noise_frames ? (s_noise_rows_total / s_noise_frames) : 0,
             s_hi_energy,
             s_lo_energy,
             s_hi_jumps,
             s_lo_jumps,
             s_bad_rows,
             JPEG_QUALITY,
             (uint32_t)(s_jpg_encode_us > 0 ? 1000000 / s_jpg_encode_us : 0),
             audio_board_is_ready() ? "true" : "false",
             AUDIO_SAMPLE_RATE,
             time_sync_is_synced() ? "true" : "false",
             (long long)time_sync_epoch_ms(),
             /* §3.9 */
             hb.rssi,
             (unsigned)hb.free_heap,
             (unsigned)hb.free_psram,
             (reliability_get_mode() == RELIABILITY_MODE_DEGRADED) ? "degraded" : "normal",
             hb.camera_ok ? "true" : "false",
             /* 电源 / 电池 */
             psrc,
             pst.charging ? "true" : "false",
             pst.percent,
             pst.mv,
             pst.valid ? "true" : "false",
             ps.connected ? "true" : "false",
             (unsigned)ps.sent_events,
             (unsigned)ps.sent_jpegs,
             (unsigned)ps.replayed,
             (unsigned)ps.dropped,
             (unsigned)ps.queued,
             (unsigned)ps.connect_fails,
             (unsigned)ps.backoff_ms,
             /* AI 对话 */
             ai_client_state_str(),
             ai_client_is_connected() ? "true" : "false",
             (unsigned)ais.turns,
             (unsigned)ais.replies,
             (unsigned)ais.errors,
             ais.last_user,
             ais.last_reply,
             /* 唤醒词「晴老师」 */
             wake_word_is_ready() ? "true" : "false",
             (unsigned)wake_word_hit_count(),
             (double)wake_word_last_prob(),
             WAKE_WORD_NAME,
             /*
              * 【2026-11-12】voice_mode: 当前语音模式 ("care" / "xiaozhi")。
              *
              * 为什么必须告诉 PC 端:
              *   板子上有【两套大脑】——
              *     看护对讲  -> 自家网关 /v1/turn (DeepSeek)
              *     小智聊天  -> 本地小智 WebSocket (你电脑上的服务)
              *   PC 端的对话桥要按这个字段挑后端, 否则两边都用同一个,
              *   "小智"就名不副实了。
              *
              * voice_mode_get() 顺手做一次空闲超时回退, 所以这里拿到的是
              * 真正生效的模式。
              */
             voice_mode_get() == VOICE_MODE_XIAOZHI ? "xiaozhi" : "care");

    httpd_resp_set_type(req, "application/json; charset=utf-8");
    httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/*
 * GET /activity —— 活动检测当前状态 (需求文档 §3.5)
 *
 * 返回格式严格按文档:
 *   { "event":"activity", "human_present":..., "motion":...,
 *     "low_light":..., "motion_ratio":..., "score":...,
 *     "last_active_ts":..., ... }
 *
 * 这个端点是【只读的事实快照】, 不做任何超时判断 ——
 * 阈值判断属于后台规则引擎 (需求文档 §2.3 原则 1)。
 */
static esp_err_t activity_handler(httpd_req_t *req)
{
    char resp[512];

    int n = activity_detect_to_json(resp, sizeof(resp));
    if (n <= 0) {
        httpd_resp_set_type(req, "application/json; charset=utf-8");
        httpd_resp_send(req, "{\"error\":\"activity not initialized\"}",
                        HTTPD_RESP_USE_STRLEN);
        return ESP_OK;
    }

    httpd_resp_set_type(req, "application/json; charset=utf-8");
    httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/*
 * ==================== 音频传输链路 ====================
 *
 * 需求文档 3.6 的规定:
 *   上行 (老人麦克风 -> 云): G.711 A-law 16kHz 单声道, 20ms 一包 (320 字节)
 *   下行 (云 -> 设备喇叭):    PCM16 16kHz 单声道, 20ms 一包 (640 字节)
 *
 * 这里用 HTTP 实现 (WSS 那套要等联网阶段), 端点设计:
 *
 *   GET  /audio/alaw    上行流: G.711 A-law 16k 单声道, 裸字节流
 *                       (Content-Type: audio/basic, 每 20ms 一包)
 *                       这是最接近正式链路的形式 —— 客户端拿到就能直接喂给
 *                       播放器或转码器。
 *
 *   GET  /audio/pcm     下行流: PCM16 16k 单声道 (audio/L16)
 *                       客户端 POST 过来的语音通过 /audio/down 灌进来,
 *                       这里负责从设备喇叭播出来。
 *
 *   POST /audio/down    下行注入: body 是 PCM16 16k 单声道, 立即播放
 *                       这是"按住说话"的落地方式 —— 客户端一边 POST 一边
 *                       设备一边播, 天然半双工。
 *
 *   GET  /audio/level   读当前录音电平 (JSON), 调试用
 *
 * 半双工互斥:
 *   下行播放期间, 上行流会被静音 (audio_board_set_downlink_active),
 *   避免喇叭回声被麦克风拾回去 —— 这是啸叫的直接来源。
 */

/* 每次 20ms @16kHz = 320 帧 */
#define AUDIO_PKT_FRAMES   320

/*
 * GET /audio/alaw —— 上行 G.711 A-law 流
 */
static esp_err_t audio_alaw_handler(httpd_req_t *req)
{
    if (!audio_board_is_ready()) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "audio not ready");
        return ESP_FAIL;
    }

    /*
     * 【2026-11-06】音频上行也是长连接, 和 /stream 共享 socket 配额。
     * 见 long_conn_acquire() 的注释: 客户端槽位只有 4 个,
     * 长连接必须限流, 否则 /health /event 这些短请求会全被挤掉。
     */
    if (!long_conn_acquire()) {
        ESP_LOGW(TAG, "长连接配额已满 (%d)，拒绝音频上行", LONG_CONN_MAX);
        httpd_resp_set_status(req, "503 Service Unavailable");
        httpd_resp_set_type(req, "text/plain; charset=utf-8");
        httpd_resp_sendstr(req, "too many long connections");
        return ESP_FAIL;
    }

    httpd_resp_set_type(req, "audio/basic");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    /* 告诉客户端这是 8bit A-law, 16kHz, 单声道 */
    httpd_resp_set_hdr(req, "X-Audio-Format", "g711-alaw;rate=16000;channels=1");

    int16_t *pcm = heap_caps_malloc(AUDIO_PKT_FRAMES * sizeof(int16_t),
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    uint8_t *alaw = heap_caps_malloc(AUDIO_PKT_FRAMES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pcm == NULL || alaw == NULL) {
        free(pcm);
        free(alaw);
        long_conn_release();
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "no memory");
        return ESP_FAIL;
    }

    while (1) {
        int n = audio_board_read_mono(pcm, AUDIO_PKT_FRAMES);
        if (n <= 0) {
            /* 环形缓冲暂时空了, 等 20ms 再试 */
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

        g711_alaw_encode_block(pcm, alaw, n);

        esp_err_t ret = httpd_resp_send_chunk(req, (const char *)alaw, n);
        if (ret != ESP_OK) {
            break;      /* 客户端断开 */
        }

        /*
         * 按真实速率限速: n 帧 @16kHz = n/16 ms。
         * 不限速的话会瞬间把环形缓冲抽干, 上游没数据可发, 客户端收到断流。
         */
        vTaskDelay(pdMS_TO_TICKS((n * 1000) / AUDIO_SAMPLE_RATE));
    }

    free(pcm);
    free(alaw);
    long_conn_release();
    httpd_resp_send_chunk(req, NULL, 0);
    return ESP_OK;
}

/*
 * POST /audio/down —— 下行注入 (PCM16 16k 单声道), 立即播放
 *
 * 客户端"按住说话"时持续 POST, 设备这边持续播 —— 半双工。
 */
static esp_err_t audio_down_handler(httpd_req_t *req)
{
    if (!audio_board_is_ready()) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "audio not ready");
        return ESP_FAIL;
    }

    int total = req->content_len;
    if (total <= 0) {
        httpd_resp_set_type(req, "application/json");
        httpd_resp_sendstr(req, "{\"ok\":false,\"err\":\"empty body\"}");
        return ESP_FAIL;
    }

    audio_board_set_downlink_active(true);

    int16_t *pcm = heap_caps_malloc(AUDIO_PKT_FRAMES * sizeof(int16_t),
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pcm == NULL) {
        audio_board_set_downlink_active(false);
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "no memory");
        return ESP_FAIL;
    }

    int played_frames = 0;
    int received = 0;

    while (received < total) {
        int want = total - received;
        if (want > AUDIO_PKT_FRAMES * (int)sizeof(int16_t)) {
            want = AUDIO_PKT_FRAMES * (int)sizeof(int16_t);
        }

        int got = httpd_req_recv(req, (char *)pcm, want);
        if (got <= 0) {
            if (got == HTTPD_SOCK_ERR_TIMEOUT) {
                continue;   /* 只是超时, 客户端可能还在发 */
            }
            break;          /* 真断开 */
        }

        int frames = got / (int)sizeof(int16_t);
        audio_board_play_mono(pcm, frames);
        played_frames += frames;
        received += got;

        /*
         * 限速: 按实际播放的帧数延时, 让 I2S TX 有时间消化。
         * 不限速会一次灌太多, i2s_channel_write 超时。
         */
        vTaskDelay(pdMS_TO_TICKS((frames * 1000) / AUDIO_SAMPLE_RATE));
    }

    free(pcm);
    audio_board_set_downlink_active(false);

    /*
     * 注意: ESP-IDF 的 httpd 响应体【只能发一次】。
     * 第一次 httpd_resp_sendstr() 会锁定响应 (设置 Content-Length 并标记
     * 已发送), 后续的调用会被静默丢弃 —— 结果就是客户端收到
     * '{"ok":true,"played_frames":' 这种被截断的 JSON。
     *
     * 所以必须拼成完整的一段再一次性发出去。
     */
    char resp[64];
    snprintf(resp, sizeof(resp), "{\"ok\":true,\"played_frames\":%d}", played_frames);
    httpd_resp_set_type(req, "application/json; charset=utf-8");
    httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/*
 * GET /audio/level —— 当前录音电平 (JSON)
 */
static esp_err_t audio_level_handler(httpd_req_t *req)
{
    audio_board_stats_t st;
    memset(&st, 0, sizeof(st));
    audio_board_get_stats(&st);

    char resp[192];
    snprintf(resp, sizeof(resp),
             "{\"peak\":%u,\"rms\":%u,\"speaking\":%s,\"samples\":%u,"
             "\"frames\":%u,\"rate\":%d,\"downlink\":%s}",
             (unsigned)st.peak, (unsigned)st.rms,
             st.speaking ? "true" : "false",
             (unsigned)st.samples, (unsigned)st.frames,
             AUDIO_SAMPLE_RATE,
             audio_board_is_downlink_active() ? "true" : "false");
    httpd_resp_set_type(req, "application/json; charset=utf-8");
    httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/*
 * GET /audio/pcm —— 保留的原始 PCM 流 (双声道 L16), 调试用。
 * 正式上行用 /audio/alaw (单声道 + G.711, 带宽减半)。
 */
static esp_err_t audio_handler(httpd_req_t *req)
{
    if (!audio_board_is_ready()) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "audio not ready");
        return ESP_FAIL;
    }

    char hdr[128];
    snprintf(hdr, sizeof(hdr), "audio/L16; rate=%d; channels=2", AUDIO_SAMPLE_RATE);
    httpd_resp_set_type(req, hdr);
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");

    /* 每次发 20ms 的数据 (320 帧 @16kHz = 1280 字节) */
    const int chunk_frames = 320;
    int16_t *buf = heap_caps_malloc(chunk_frames * 4, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (buf == NULL) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "no memory");
        return ESP_FAIL;
    }

    /* 客户端断开时 httpd 会让 send 返回错误, 循环退出 */
    while (1) {
        int n = audio_board_read(buf, chunk_frames);
        if (n <= 0) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        char range[64];
        snprintf(range, sizeof(range), "%d", n * 4);
        httpd_resp_set_hdr(req, "Content-Length", range);

        esp_err_t ret = httpd_resp_send_chunk(req, (const char *)buf, n * 4);
        if (ret != ESP_OK) {
            break;      /* 客户端断开 */
        }
    }

    free(buf);
    httpd_resp_send_chunk(req, NULL, 0);
    return ESP_OK;
}

static void httpd_control_start(void);

static void http_server_start(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = HTTP_PORT;
    config.max_uri_handlers = 16;

    /*
     * 【2026-11-12 修复"内部 RAM 耗尽 -> 所有任务建不起来"】
     *
     * 现场症状: 内部 RAM 只剩 303 字节, 于是
     *     任务 button_task 的 TCB 分配失败 (内部RAM)
     *     任务 cam_producer 的 TCB 分配失败 (内部RAM)
     *     任务 uart_cmd 的 TCB 分配失败 (内部RAM)
     * 连带 i2c 命令链也分配不出来, 最后 StoreProhibited 崩溃。
     *
     * 内存快照定位到的元凶:
     *     [MEM] http_server_start 后  internal=17483   <- 之前
     *     [MEM] 建完基础任务后        internal=303     <- 之后
     * 也就是说 httpd 起来后内部 RAM 就只剩 17KB 了。
     *
     * 两个 httpd 实例的主任务栈都在【内部 RAM】:
     *     主实例    stack_size = 10240
     *     控制面    stack_size = 4096
     * 合计 14KB —— 内部 RAM 只有 200 多 KB, 这一下就吃掉一大块。
     *
     * 修法:
     *   1. task_caps 改成 MALLOC_CAP_SPIRAM —— httpd 任务栈挪到 PSRAM。
     *      (IDF 5.3 的 httpd_config_t 就有这个字段, 默认是 INTERNAL)
     *   2. 栈从 10240 降到 8192 —— /stream handler 只是读 JPEG 槽位
     *      然后 chunked 发送, 8KB 绰绰有余。
     *
     * 为什么栈可以放 PSRAM: httpd 任务钉在 core 0 (config.core_id),
     * 跟 cam_task 同核。ESP32-S3 的 PSRAM 缓存是【按核】维护的,
     * 同核访问不会读到陈旧数据 —— 这也是为什么 httpd 必须钉 core 0。
     */
    config.task_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
    config.stack_size = 8192;
    /*
     * 【2026-11-06 修复"板子 HTTP 全部 502"】
     *
     * HTTPD_DEFAULT_CONFIG() 的 max_open_sockets 只有 7。而 /stream 和
     * /audio/alaw 都是【长连接】—— 一个浏览器标签页就能占掉 1 个 socket,
     * 代理端再重试几次, 槽位就被占光, 结果 /health /activity 这些短请求
     * 也排队 -> 整个 HTTP 服务看着像挂了。
     *
     * 现场证据: 控制台代理反复重连 /stream, 板子随后 /health 全部 502,
     *           但串口日志显示相机还在跑 (5.8 fps) —— 说明板子活着, 只是
     *           HTTP 槽位耗尽。
     *
     * ⚠️ 注意: ESP-IDF 对这个值有【硬上限】。IDF 5.3 的 httpd 内部固定
     *    占用 3 个 socket, 最多只能再开 4 个给客户端, 所以 max_open_sockets
     *    最大只能填 7。填 12 会直接:
     *        E httpd: Config option max_open_sockets is too large
     *                 (max allowed 7, 3 sockets used by HTTP server internally)
     *        E kanhu: HTTP 服务器启动失败: ESP_ERR_INVALID_ARG
     *    —— 整个 HTTP 服务直接起不来 (这正是本次踩的坑)。
     *
     * 所以光加 socket 数量不够, 必须同时:
     *   1) 代理端只维持【一条】上游长连接 (见 pc_receiver/pc_console.py
     *      的 SharedStream), 多个浏览器标签页共用;
     *   2) 短请求不能被长连接挤掉 —— 见下面的 lru_purge_enable。
     */
    config.max_open_sockets = 7;   /* IDF 硬上限, 不能再大 */
    /*
     * 【2026-11-06】开 LRU 回收: 客户端异常断开后, socket 不立刻归还,
     * 靠 LRU 超时淘汰, 否则反复重连会持续消耗槽位。
     */
    config.lru_purge_enable = true;
    /*
     * 【2026-11-06】HTTP 超时必须设, 否则一个卡住的长连接会永久占住
     * 一个 worker。recv_wait_timeout 是等首字节, send_wait_timeout 是等
     * 发送完成。10s 足够正常传输, 又不会让死连接长期占坑。
     */
    config.recv_wait_timeout = 10;
    config.send_wait_timeout = 10;
    /*
     * 钉到 core 0 —— 和 cam_task / 其它任务同核。
     *
     * 原因: httpd 的 stream_handler 会读 PSRAM 里的 JPEG 槽位。
     * ESP32-S3 的 PSRAM 缓存每核独立, 如果 httpd 跑在 core 1,
     * 它读到的可能是 core 0 写入前的陈旧缓存 -> 浏览器端画面撕裂。
     * 之前"浏览器干净、LCD 花屏"就是任务分散在不同核上的结果。
     */
    config.core_id = APP_CORE_ID;

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
    httpd_uri_t raw = { .uri = "/raw", .method = HTTP_GET, .handler = raw_handler, .user_ctx = NULL };
    httpd_uri_t audio = { .uri = "/audio", .method = HTTP_GET, .handler = audio_handler, .user_ctx = NULL };
    httpd_uri_t audio_alaw = { .uri = "/audio/alaw", .method = HTTP_GET, .handler = audio_alaw_handler, .user_ctx = NULL };
    httpd_uri_t audio_down = { .uri = "/audio/down", .method = HTTP_POST, .handler = audio_down_handler, .user_ctx = NULL };
    httpd_uri_t audio_level = { .uri = "/audio/level", .method = HTTP_GET, .handler = audio_level_handler, .user_ctx = NULL };
    httpd_uri_t activity = { .uri = "/activity", .method = HTTP_GET, .handler = activity_handler, .user_ctx = NULL };
    httpd_uri_t cmd = { .uri = "/cmd", .method = HTTP_POST, .handler = cmd_handler, .user_ctx = NULL };

    httpd_register_uri_handler(server, &root);
    httpd_register_uri_handler(server, &stream);
    httpd_register_uri_handler(server, &ev);
    httpd_register_uri_handler(server, &notify);
    httpd_register_uri_handler(server, &health);
    httpd_register_uri_handler(server, &raw);
    httpd_register_uri_handler(server, &audio);
    httpd_register_uri_handler(server, &audio_alaw);
    httpd_register_uri_handler(server, &audio_down);
    httpd_register_uri_handler(server, &audio_level);
    httpd_register_uri_handler(server, &activity);
    httpd_register_uri_handler(server, &cmd);

    ESP_LOGI(TAG, "HTTP 服务器已启动，端口 %d", HTTP_PORT);
    ESP_LOGI(TAG, "音频端点: /audio/alaw (上行G711) /audio/down (下行PCM) /audio/level (电平)");
    ESP_LOGI(TAG, "活动检测端点: /activity (motion / human_present / low_light)");
    ESP_LOGI(TAG, "家人指令端点: POST /cmd (watching / calling / content)");

    httpd_control_start();
}

/*
 * ---------------------------------------------------------------------------
 * 【2026-11-06 新增】控制面 HTTP 服务器 (端口 8081) —— 数据面/控制面分离
 * ---------------------------------------------------------------------------
 *
 * 【要解决的问题】
 *   主服务器(8080)上既有 /stream 这种【长连接大流量】, 又有 /health 这种
 *   【短请求】。ESP-IDF 的 http_server:
 *     - 客户端 socket 槽位 = max_open_sockets - 3 (内部占用), 上限只有 4
 *     - 每个活跃请求占用 1 个 socket + 1 个 httpd worker 线程
 *   /stream 一个客户端就长期占住这两个资源。
 *   几个客户端一连上, /health /activity /event 就全部排队甚至超时 ——
 *   表现就是"板子像挂了", 但串口里相机其实还在正常跑 (5.8 fps)。
 *
 *   实测事故: 3 路浏览器 + 代理重连 -> 板子 /health 连续超时,
 *   而串口 PROD 日志一直显示 5.8fps 正常出图。
 *
 * 【解法: 控制面/数据面分离】
 *   8080  数据面: /stream /raw /audio/alaw /audio/down (长连接, 允许被占满)
 *   8081  控制面: /health /activity /event /notify /cmd (短请求, 独立 server)
 *
 *   两个 server 各有自己的 socket 池和 worker 池, 互不影响。
 *   数据面被 MJPEG 占满时, 控制面依然能立刻响应 —— 这正是"看护"场景
 *   需要的: 画面卡可以接受, 但"查状态/收告警"绝不能卡。
 *
 * 【必须配不同的 ctrl_port】
 *   IDF 的 httpd 用一个 UDP ctrl socket 做内部事件通知, 默认端口 32768
 *   (ESP_HTTPD_DEF_CTRL_PORT)。两个实例共用它 -> bind 失败 errno 112:
 *       E httpd: httpd_server_init: error in creating ctrl socket (112)
 *   httpd_config_t.ctrl_port 就是为此准备的, 给第二个实例配 32769 即可。
 */
static void httpd_control_start(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = HTTP_CONTROL_PORT;
    config.ctrl_port = HTTP_CONTROL_CTRL_PORT;   /* 必须不同于主实例的 32768 */
    /*
     * ⚠️ max_uri_handlers 必须 >= 实际注册的 URI 数量, 否则超出的注册
     *    会【静默失败】(httpd_register_uri_handler 返回错误但这里没检查),
     *    表现为该端点 404 "Nothing matches the given URI"。
     *
     * 目前控制面注册 9 个:
     *   /health /activity /event /notify /cmd /root
     *   /audio/level /audio/alaw /audio/down
     * 所以至少要 12 留点余量。
     */
    config.max_uri_handlers = 12;
    /* 【2026-11-12】栈也挪到 PSRAM, 理由同 http_server_start()。 */
    config.task_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
    config.stack_size = 4096;          /* 控制面 handler 很轻, 栈可以小 */
    config.max_open_sockets = 6;       /* 控制面: 短请求 + 音频长连接 */
    config.lru_purge_enable = true;
    config.recv_wait_timeout = 5;
    config.send_wait_timeout = 5;
    config.core_id = APP_CORE_ID;

    httpd_handle_t server = NULL;
    esp_err_t ret = httpd_start(&server, &config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "控制面 HTTP 服务器启动失败: %s", esp_err_to_name(ret));
        return;
    }

    httpd_uri_t health = { .uri = "/health", .method = HTTP_GET, .handler = health_handler, .user_ctx = NULL };
    httpd_uri_t activity = { .uri = "/activity", .method = HTTP_GET, .handler = activity_handler, .user_ctx = NULL };
    httpd_uri_t ev = { .uri = "/event", .method = HTTP_GET, .handler = event_handler, .user_ctx = NULL };
    httpd_uri_t notify = { .uri = "/notify", .method = HTTP_POST, .handler = notify_handler, .user_ctx = NULL };
    httpd_uri_t cmd = { .uri = "/cmd", .method = HTTP_POST, .handler = cmd_handler, .user_ctx = NULL };
    httpd_uri_t root = { .uri = "/", .method = HTTP_GET, .handler = root_handler, .user_ctx = NULL };

    /*
     * 音频端点也注册到控制面。
     *
     * 为什么要重复注册: 音频上行 /audio/alaw 是长连接, 下行 /audio/down
     * 是大 body 的 POST。两者原本都在数据面(8080), 而数据面被 MJPEG 占满时
     * 它们就完全没法用 —— 实测"看画面"和"对讲"无法同时进行, 这对看护场景
     * 是不可接受的 (画面可以卡, 但子女要能随时喊话)。
     *
     * 控制面的 socket 槽位和 worker 池是独立的, 所以音频在这里不受
     * MJPEG 影响。同一端口在两个 server 上注册不同的 URI 不冲突。
     */
    httpd_uri_t audio_level = { .uri = "/audio/level", .method = HTTP_GET, .handler = audio_level_handler, .user_ctx = NULL };
    httpd_uri_t audio_alaw = { .uri = "/audio/alaw", .method = HTTP_GET, .handler = audio_alaw_handler, .user_ctx = NULL };
    httpd_uri_t audio_down = { .uri = "/audio/down", .method = HTTP_POST, .handler = audio_down_handler, .user_ctx = NULL };

    /*
     * 【2026-11-06】校验关键端点是否真的注册上了。
     *
     * httpd_register_uri_handler 失败时【不报错】, 只是那个 URI 404。
     * 之前 max_uri_handlers 设成 8 而实际注册 9 个, /audio/down 就被静默
     * 丢掉了, 表现为对讲下发 404 —— 极难排查。
     * 这里把每个注册结果都打出来, 一眼就能看出少了哪个。
     */
    struct { const char *uri; esp_err_t err; } regs[] = {
        {"/health",       httpd_register_uri_handler(server, &health)},
        {"/activity",     httpd_register_uri_handler(server, &activity)},
        {"/event",        httpd_register_uri_handler(server, &ev)},
        {"/notify",       httpd_register_uri_handler(server, &notify)},
        {"/cmd",          httpd_register_uri_handler(server, &cmd)},
        {"/",             httpd_register_uri_handler(server, &root)},
        {"/audio/level",  httpd_register_uri_handler(server, &audio_level)},
        {"/audio/alaw",   httpd_register_uri_handler(server, &audio_alaw)},
        {"/audio/down",   httpd_register_uri_handler(server, &audio_down)},
    };
    int reg_fail = 0;
    for (size_t i = 0; i < sizeof(regs) / sizeof(regs[0]); i++) {
        if (regs[i].err != ESP_OK) {
            ESP_LOGE(TAG, "控制面端点 %s 注册失败: %s",
                     regs[i].uri, esp_err_to_name(regs[i].err));
            reg_fail++;
        }
    }
    if (reg_fail == 0) {
        ESP_LOGI(TAG, "控制面 %d 个端点全部注册成功",
                 (int)(sizeof(regs) / sizeof(regs[0])));
    }

    ESP_LOGI(TAG, "控制面 HTTP 服务器已启动，端口 %d", HTTP_CONTROL_PORT);
    ESP_LOGI(TAG, "  控制面端点: /health /activity /event /notify /cmd");
    ESP_LOGI(TAG, "  控制面音频: /audio/alaw /audio/down /audio/level  (与画面互不干扰)");
}

/* ---------------- 内存快照 ---------------- */

/*
 * 打印内部 RAM 占用。
 *
 * 【为什么必须看内部 RAM 而不是 esp_get_free_heap_size()】
 * 后者统计的是总堆 (含 8MB PSRAM), 永远看起来很充裕, 但内部 RAM
 * 可能早就被吃光了 —— 任务 TCB 分配失败、i2c command link 分配失败
 * 都是内部 RAM 耗尽的症状, 而总堆数字完全看不出问题。
 *
 * 【为什么用 printf 而不是 ESP_LOGI】
 * 开机早期的日志会被后面密集的日志刷掉 (串口是 115200, 一秒能传几 KB),
 * 内存快照恰恰发生在最早期, 用 ESP_LOGI 常常来不及看。
 * printf 走的是另一个缓冲, 而且不受 log level 影响。
 */
static void mem_snapshot(const char *tag)
{
    printf("[MEM] %-22s internal=%u min=%u total=%u\n",
           tag,
           (unsigned int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned int)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
           (unsigned int)esp_get_free_heap_size());
}

/* ---------------- 任务创建辅助 ---------------- */

/*
 * 内部 RAM 很紧张 (Wi-Fi 驱动 + 相机 + LCD 都要内部内存)，
 * FreeRTOS 默认从内部 RAM 分任务栈，很容易 xTaskCreate 失败
 * (errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY = -1)。
 * 所以这里显式从 PSRAM 分配栈，再手动建任务。
 *
 * 全部任务钉在 core 0 (APP_CORE_ID), 原因见 APP_CORE_ID 的注释:
 * 保证和 cam_task 共用同一份 PSRAM 缓存, 否则读到陈旧帧 -> 花屏。
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
        /*
         * 内部 RAM 不够了。打印详细内存情况 —— 光说"失败"没法排查,
         * 得知道还剩多少、是不是碎片化的。
         */
        ESP_LOGE(TAG, "任务 %s 的 TCB 分配失败 (内部RAM), 内部剩余=%u 最小剩余=%u 总堆剩余=%u",
                 name,
                 (unsigned int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned int)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned int)esp_get_free_heap_size());
        heap_caps_free(stack);
        return;
    }

    TaskHandle_t handle = xTaskCreateStaticPinnedToCore((TaskFunction_t)fn, name, stack_bytes,
                                                        NULL, prio, stack, tcb, APP_CORE_ID);
    if (handle == NULL) {
        ESP_LOGE(TAG, "任务 %s 创建失败", name);
        heap_caps_free(stack);
        heap_caps_free(tcb);
        return;
    }
    ESP_LOGI(TAG, "任务 %s 已启动 (core %d)", name, APP_CORE_ID);
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

/*
 * ==================== Wi-Fi 扫描 (排障/选网用) ====================
 *
 * 为什么必须有这个:
 *   电脑的 netsh 只能看到 5GHz 的 "WMU-EDU" (信道 52), 而 ESP32-S3
 *   只支持 2.4GHz —— 所以板子连不上, 但我们不知道附近到底有没有
 *   同名的 2.4GHz BSSID, 也不知道还有哪些 2.4GHz 网络可用。
 *
 *   让板子自己扫一遍, 是唯一可靠的办法 —— 它的射频和 PC 不一样。
 *
 * 用法: 串口命令 "scan"
 */

static void wifi_scan_and_report(void)
{
    /*
     * Wi-Fi 没开的话, 现场初始化射频 (只 start, 不 connect)。
     * 这样即使固件是 WIFI_ENABLE=0 的诊断状态, 也能随时扫网。
     */
    {
        wifi_mode_t mode = WIFI_MODE_NULL;
        if (esp_wifi_get_mode(&mode) != ESP_OK || mode == WIFI_MODE_NULL) {
            ESP_LOGI(TAG, "Wi-Fi 未初始化, 现在启动射频 (仅扫描)");
            wifi_scan_init();
        }
    }

    /*
     * 【2026-11-12 修复"scan 永远失败"】
     *
     * 现象: 扫不到任何网络, 或者报
     *     E kanhu: 扫描启动失败: ESP_ERR_WIFI_STATE
     *
     * 原因: ESP32 的 Wi-Fi 驱动【STA 正在连接/重连时不允许扫描】。
     * 我们的固件是 STA 模式, 而且板子连不上网 -> 一直在退避重连,
     * 刚好撞上扫描命令 -> ESP_ERR_WIFI_STATE。
     *
     * 修法: 扫描前先断开 STA (会触发一次 DISCONNECTED 事件, 走退避
     * 重连逻辑, 那个逻辑本身是安全的), 再等驱动进入空闲态, 然后扫。
     * 扫完不必马上重连 —— 重连任务会在退避计时到期后自动接上。
     */
    {
        wifi_mode_t mode = WIFI_MODE_NULL;
        if (esp_wifi_get_mode(&mode) == ESP_OK && mode == WIFI_MODE_STA) {
            ESP_LOGI(TAG, "扫描前先断开 STA 连接 (驱动不允许边连边扫)");
            esp_wifi_disconnect();
            /* 等驱动从"连接中"退出来。给 500ms 余量。 */
            vTaskDelay(pdMS_TO_TICKS(500));
        }
    }

    wifi_scan_config_t scan_cfg = {
        .show_hidden = false,
        .scan_type   = WIFI_SCAN_TYPE_ACTIVE,
        /*
         * 每信道扫描时间。active 模式 min=0/max=120ms 是 IDF 默认值,
         * 但对"找出信号最强的那个"来说偏短 —— 主动探测要等 AP 的
         * Probe Response, 信道切换多的时候容易漏。这里放宽一点。
         */
        .scan_time = {
            .active = { .min = 60, .max = 200 },
            .passive = 300,
        },
    };

    ESP_LOGI(TAG, "开始扫描 2.4GHz Wi-Fi (最多 8 秒)...");

    esp_err_t ret = esp_wifi_scan_start(&scan_cfg, true);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "扫描启动失败: %s", esp_err_to_name(ret));
        return;
    }

    uint16_t found = 0;
    ret = esp_wifi_scan_get_ap_num(&found);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "读取扫描结果数量失败: %s", esp_err_to_name(ret));
        return;
    }
    if (found == 0) {
        ESP_LOGW(TAG, "附近没有扫描到任何 2.4GHz 网络");
        return;
    }

    ESP_LOGI(TAG, "扫描到 %u 个 2.4GHz 网络:", found);

    /* ap_info_t 每个约 100 字节, 20 个就 2KB。
     * 【2026-11-12】改放 PSRAM —— 内部 RAM 现在只剩 21KB, 经不起这种
     * "临时诊断用的"分配。之前放内部 RAM, 扫一次网就把它榨干。 */
    wifi_ap_record_t *recs = heap_caps_malloc(found * sizeof(wifi_ap_record_t),
                                              MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (recs == NULL) {
        ESP_LOGE(TAG, "扫描结果缓冲分配失败 (%u 字节)", (unsigned)(found * sizeof(wifi_ap_record_t)));
        return;
    }

    ret = esp_wifi_scan_get_ap_records(&found, recs);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "读取扫描记录失败: %s", esp_err_to_name(ret));
        free(recs);
        return;
    }

    /* 按信号强度排序, 最强的排前面 */
    for (int i = 0; i < found; i++) {
        for (int j = i + 1; j < found; j++) {
            if (recs[j].rssi > recs[i].rssi) {
                wifi_ap_record_t t = recs[i];
                recs[i] = recs[j];
                recs[j] = t;
            }
        }
    }

    for (int i = 0; i < found && i < 20; i++) {
        const char *auth = "OPEN";
        switch (recs[i].authmode) {
        case WIFI_AUTH_WPA_WPA2_PSK: auth = "WPA2"; break;
        case WIFI_AUTH_WPA2_PSK:     auth = "WPA2"; break;
        case WIFI_AUTH_WPA3_PSK:     auth = "WPA3"; break;
        case WIFI_AUTH_WPA2_ENTERPRISE: auth = "WPA2-ENT"; break;
        case WIFI_AUTH_WPA_ENTERPRISE:      auth = "WPA-ENT"; break;
        default: break;
        }
        ESP_LOGI(TAG, "  [%2d] %-24s ch%-3d %4d dBm  %s  %s",
                 i,
                 (char *)recs[i].ssid,
                 recs[i].primary,
                 recs[i].rssi,
                 auth,
                 (i == 0) ? "<- 最强" : "");
    }

    free(recs);
}

/* ---------------- 板载自环测试 ---------------- */

/*
 * 在板子内部完整跑一遍音频传输链路, 不需要外部电脑。
 *
 * 背景: 校园网 (WMU-EDU) 开了客户端隔离, PC 访问板子的 IP 会被丢包,
 * 所以"PC 抓流 -> PC 灌流"这种外部测试做不了。但链路本身在板子上
 * 是通的 —— 板子访问自己的 127.0.0.1:8080 完全正常。
 *
 * 所以让板子自己当客户端:
 *   1) 开一个 http client 连 http://127.0.0.1:8080/audio/alaw
 *      -> 验证 G.711 上行端点真的在吐数据, 且字节流可解码
 *   2) 把收到的 A-law 数据在板子上解码回 PCM
 *      -> 验证"编码 -> 网络 -> 解码"整条路的数据完整性
 *   3) 开一个 http client POST PCM 到 http://127.0.0.1:8080/audio/down
 *      -> 验证下行端点真的接收并播放
 *
 * 用法: 串口命令 "axtest"
 */
static void audio_selftest_loop(void)
{
#if !WIFI_ENABLE
    ESP_LOGE(TAG, "自环测试需要 Wi-Fi (自环也要走 TCP/IP 协议栈)");
    return;
#else
    esp_netif_ip_info_t ip;
    if (esp_netif_get_ip_info(s_netif, &ip) != ESP_OK || ip.ip.addr == 0) {
        ESP_LOGE(TAG, "自环测试需要先连上 Wi-Fi (当前没 IP)");
        return;
    }

    char host[32];
    /*
     * 注意: esp_ip4_addr_t.addr 已经是网络字节序, 不能按字节拆开打印 ——
     * 我第一版写成 (addr >> 24) & 0xFF, 结果把 172.72.49.211 打成了
     * 211.49.72.172。用 IP2STR 宏, 它会正确处理字节序。
     */
    snprintf(host, sizeof(host), IPSTR, IP2STR(&ip.ip));
    ESP_LOGI(TAG, "自环测试开始 (目标 %s:8080)", host);

    /* ---------- 步骤 1+2: 上行 G.711 链路 ---------- */
    {
        esp_http_client_config_t cfg = {
            .url = "http://127.0.0.1:8080/audio/alaw",
            .timeout_ms = 10000,
        };
        esp_http_client_handle_t cli = esp_http_client_init(&cfg);
        if (cli == NULL) {
            ESP_LOGE(TAG, "自环: http client 初始化失败");
            return;
        }

        esp_err_t ret = esp_http_client_open(cli, 0);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "自环: 连接 /audio/alaw 失败 (%s)", esp_err_to_name(ret));
            esp_http_client_cleanup(cli);
            return;
        }

        int total_alaw = 0;
        int total_pcm  = 0;
        int peak       = 0;
        long long sum_sq = 0;

        /*
         * 缓冲必须放堆上, 不能放栈上!
         * 这个函数是在 uart_cmd 任务里调的, 那个任务栈只有 4096 字节,
         * 两个 1KB 的数组 + esp_http_client 自己的栈就爆了
         * (实测直接 stack overflow 重启)。
         */
        uint8_t *buf = heap_caps_malloc(512, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        int16_t *pcm = heap_caps_malloc(512 * sizeof(int16_t),
                                         MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (buf == NULL || pcm == NULL) {
            ESP_LOGE(TAG, "自环: 缓冲分配失败");
            free(buf);
            free(pcm);
            esp_http_client_close(cli);
            esp_http_client_cleanup(cli);
            return;
        }

        uint32_t start = esp_timer_get_time();

        /*
         * 【修复】原来这里没调 esp_http_client_fetch_headers, 直接就
         * esp_http_client_read。/audio/alaw 是 chunked 响应 (httpd_resp_send_chunk),
         * 响应头没解析出来时 client->response->is_chunked 还是 0,
         * content_length 也是 0 -> read 认为"没有数据", 立刻返回 0,
         * 于是自环[上行] 永远收到 0 字节。
         *
         * 必须先 fetch_headers 把响应头解析完, 才知道这是 chunked 流。
         */
        int64_t hdr = esp_http_client_fetch_headers(cli);
        if (hdr < 0) {
            ESP_LOGE(TAG, "自环[上行]: 取响应头失败 (%d)", (int)hdr);
            esp_http_client_close(cli);
            esp_http_client_cleanup(cli);
            free(buf);
            free(pcm);
            return;
        }
        ESP_LOGI(TAG, "自环[上行]: 响应头 OK (content_length=%lld)",
                 (long long)esp_http_client_get_content_length(cli));

        while (total_alaw < 16000) {   /* 收 1 秒的数据 */
            int n = esp_http_client_read(cli, (char *)buf, 512);
            if (n <= 0) {
                break;
            }
            total_alaw += n;

            /* 在板子上解码回来 —— 验证数据没被损坏 */
            g711_alaw_decode_block(buf, pcm, n);
            for (int i = 0; i < n; i++) {
                int v = pcm[i];
                if (v < 0) v = -v;
                if (v > peak) peak = v;
                sum_sq += (long long)pcm[i] * pcm[i];
            }
            total_pcm += n;
        }
        uint32_t elapsed_us = (uint32_t)(esp_timer_get_time() - start);
        esp_http_client_close(cli);
        esp_http_client_cleanup(cli);
        free(buf);
        free(pcm);

        int rms = (total_pcm > 0) ? (int)(sqrt((double)sum_sq / total_pcm)) : 0;

        ESP_LOGI(TAG, "自环[上行]: 收到 %d 字节 A-law, 解码 %d 个 PCM 样本, 耗时 %u ms",
                 total_alaw, total_pcm, (unsigned)(elapsed_us / 1000));
        ESP_LOGI(TAG, "自环[上行]: 解码后 peak=%d rms=%d, 实际码率 %lld 字节/秒",
                 peak, rms, (long long)total_alaw * 1000000LL / (elapsed_us ? elapsed_us : 1));

        if (total_alaw == 0) {
            ESP_LOGE(TAG, "自环[上行] 失败: 一个字节都没收到");
        } else {
            /*
             * 16000 字节/秒 是设计值 (16kHz 单声道 G.711), 允许 20% 偏差。
             *
             * 【2026-11-03 修正】用 long long 算码率。
             * 原来写的是 total_alaw * 1000000, total_alaw 是 int,
             * 乘 1000000 会溢出 int32 (16000 * 1000000 = 1.6e10 早就爆了),
             * 除完再转 int 得到一个完全错误的数 —— 实测报"2008 字节/秒",
             * 实际是 16384 字节 / 1069 ms = 15326 字节/秒, 接近 16000, 链路是好的。
             */
            long long rate = (long long)total_alaw * 1000000LL /
                             (elapsed_us ? elapsed_us : 1);
            if (rate > 12800 && rate < 19200) {
                ESP_LOGI(TAG, "自环[上行] 通过: 码率 %lld 字节/秒 (设计 16000)", rate);
            } else {
                ESP_LOGW(TAG, "自环[上行] 码率异常: %lld 字节/秒 (期望 16000 附近)", rate);
            }
            if (peak > 0) {
                ESP_LOGI(TAG, "自环[上行] 音频非静音 (peak=%d) —— 麦克风在拾音", peak);
            } else {
                ESP_LOGW(TAG, "自环[上行] 解码后全零 —— 可能是安静环境, 也可能是没采到音");
            }
        }
    }

    /* ---------- 步骤 3: 下行链路 ---------- */
    {
        /*
         * 造 1 秒的 440Hz 正弦波 (16kHz 单声道 PCM16),
         * POST 到 /audio/down, 设备应该把它播出来。
         */
        const int frames = 16000;
        /*
         * 1 秒单声道 PCM16 = 32KB。放 PSRAM —— 内部 RAM 只剩 28KB,
         * 放内部必然分配失败 (这正是之前"下行: 缓冲分配失败"的原因)。
         * 这块数据只是喂给 http client, 不参与 DMA, PSRAM 完全没问题。
         */
        int16_t *pcm = heap_caps_malloc(frames * sizeof(int16_t),
                                         MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (pcm == NULL) {
            ESP_LOGE(TAG, "自环[下行]: 缓冲分配失败");
            return;
        }
        for (int i = 0; i < frames; i++) {
            double ph = 2.0 * 3.14159265358979 * 440.0 * i / AUDIO_SAMPLE_RATE;
            pcm[i] = (int16_t)(sin(ph) * 10000);
        }

        esp_http_client_config_t cfg = {
            .url = "http://127.0.0.1:8080/audio/down",
            .timeout_ms = 15000,
            .method = HTTP_METHOD_POST,
        };
        esp_http_client_handle_t cli = esp_http_client_init(&cfg);
        if (cli == NULL) {
            ESP_LOGE(TAG, "自环[下行]: http client 初始化失败");
            free(pcm);
            return;
        }

        esp_err_t ret = esp_http_client_open(cli, frames * sizeof(int16_t));
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "自环[下行]: 连接失败 (%s)", esp_err_to_name(ret));
            esp_http_client_cleanup(cli);
            free(pcm);
            return;
        }

        /* 分块发送, 每块 640 字节 = 20ms (需求文档规定的一包大小) */
        int sent = 0;
        const int chunk = 640;
        while (sent < frames * (int)sizeof(int16_t)) {
            int want = (frames * (int)sizeof(int16_t)) - sent;
            if (want > chunk) want = chunk;
            int w = esp_http_client_write(cli, (const char *)&pcm[sent / 2], want);
            if (w < 0) {
                ESP_LOGE(TAG, "自环[下行]: 发送失败");
                break;
            }
            sent += w;
        }

        /*
         * 【2026-11-03 修正】
         * 原来这里直接 esp_http_client_get_status_code(cli) 就完事,
         * 但 POST 完 body 之后响应头还没被解析, status_code 还是初始值 0,
         * 于是判断 "status == 200" 永远不成立 -> 误报"下行失败"。
         * 必须先 fetch_headers 把响应头读出来, 再取状态码。
         */
        int64_t hdr2 = esp_http_client_fetch_headers(cli);
        int status = esp_http_client_get_status_code(cli);
        ESP_LOGI(TAG, "自环[下行]: 发送 %d 字节 (1 秒 440Hz), HTTP %d (headers=%lld)",
                 sent, status, (long long)hdr2);

        /* 读响应体 (设备会回报播了多少帧) */
        char resp[128] = {0};
        int rlen = esp_http_client_read(cli, resp, sizeof(resp) - 1);
        if (rlen > 0) {
            resp[rlen] = '\0';
            ESP_LOGI(TAG, "自环[下行]: 设备响应: %s", resp);
        }
        esp_http_client_close(cli);
        esp_http_client_cleanup(cli);
        free(pcm);

        if (sent == frames * (int)sizeof(int16_t) && status == 200) {
            ESP_LOGI(TAG, "自环[下行] 通过 —— 1 秒音频已送入设备播放");
        } else {
            ESP_LOGE(TAG, "自环[下行] 失败: 发了 %d 字节, HTTP %d", sent, status);
        }
    }

    ESP_LOGI(TAG, "自环测试结束");
#endif
}

/* ---------------- 串口调试命令 ---------------- */

/*
 * 排障用的调试命令 (通过 USB 串口输入, 换行执行):
 *
 *   lcd0    显示摄像头画面 (默认)
 *   lcd1    显示固定测试图样 (不碰摄像头)  -> 判定 LCD/SPI 链路好坏
 *   lcd2    显示合成渐变图 (走 draw_rgb565 路径) -> 判定画图代码路径好坏
 *   stat    打印性能与撕裂统计
 *   q       打印【花屏量化指标】(g2_bad 最重要: 干净 <1%, 花屏 11~20%)
 *   hash    缓冲稳定性测试 (同一帧隔 250ms 算两次哈希)
 *   conv 0/1 关/开硬件 YUV->RGB 转换器 (A/B 对照, 会安全暂停生产者)
 *   mode 0/1/2/3 传感器模式: 0=YUV422+转换器 1=直出RGB565 2=灰度 3=直出YUV422(真值)
 *   gfix    开关【软件修复绿色低位】(转换器绿色低位是随机噪声, 修它最直接)
 *   gfixstat 打印 gfix 状态
 *   cbar 0/1 关/开传感器内置彩条 (黄金标准测试: 判定根因在链路还是数据解释)
 *   pclk 0/1/2/3 设 GC0308 的 PCLK 分频 (0x28): DVP 采样太快要降速
 *   spiclk N 设 LCD 的 SPI 时钟 (Hz), 用来定位"时钟太快导致花屏"
 *   pda 0/1 关/开 PSRAM DMA 直写 (A/B 对照: 哪个模式不花屏)
 *   wifioff 关掉 Wi-Fi (用来验证: 花屏是不是 PSRAM 总线争用引起的)
 *   wifix   重新打开 Wi-Fi
 *   astat   打印音频统计 (peak/rms/累计样本)
 *   tone    开关循环测试音 (toggle)
 *   vol N   设置输出音量 0-100
 *   gain N  设置麦克风输入增益 -40 ~ +40 dB
 *   astop   停掉所有音频后台任务
 *
 * 为什么用运行时命令而不是编译期宏:
 *   花屏排查需要反复切换对照, 每次改宏都要重新编译烧录 (约 2 分钟),
 *   改命令则只要敲一行, 当场就能看到结果。
 *
 * ---------------------------------------------------------------------------
 * 为什么不用 read(STDIN_FILENO) 而用 usb_serial_jtag_read_bytes()
 *
 * 之前这里用 read(STDIN_FILENO, &ch, 1) 读命令, 结果命令永远收不到。
 * 原因:
 *   CONFIG_ESP_CONSOLE_UART_DEFAULT=y  ->  IDF 的主 console 挂在 UART0
 *                                          (排针 GPIO43/44), stdout 也走它
 *   CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y -> USB-Serial-JTAG 只是
 *                                          "次要" console, 只转发输出, 不接输入
 *
 *   而我们的 COM3 正是 USB-Serial/JTAG 设备。于是:
 *     - 往 COM3 发的字节进的是 USB-Serial-JTAG 的 RX 缓冲
 *     - read(STDIN_FILENO) 读的是 UART0 (排针), 根本不是同一个地方
 *     - 而且就算读对了, 还会和 IDF console 抢 stdin, 行为不可预测
 *
 * 正确做法: 直接用 usb_serial_jtag_read_bytes() 从 USB 设备读, 和 IDF
 * console 的输出互不干扰 (console 的 stdout 走 UART0, 我们只读 USB RX)。
 * 输出仍然用 ESP_LOGI (走 IDF 的双路 console, COM3 上也能看到)。
 */
/* 前置声明: inject_button_event / button_raw_dump 定义在 uart_cmd_task 之后 */
static void inject_button_event(const char *msg);
static void button_raw_dump(int seconds);
static void aw9523b_dump_regs(void);
static void aw_key_line_test(void);
static void boot_button_simulate(void);

static void uart_cmd_task(void *arg)
{
    /*
     * 命令缓冲区放大到 256 字节。
     *
     * 原因: evtest 允许注入【中文文案】来验证 A6「文案可配置」。
     * UTF-8 里一个汉字占 3 字节, 64 字节只够放 20 个字, 太紧;
     * 256 字节能放 80+ 个汉字, 足够实际使用。
     */
    char line[256];
    int  len = 0;
    while (true) {
        uint8_t ch;
        int n = usb_serial_jtag_read_bytes(&ch, 1, pdMS_TO_TICKS(50));
        if (n != 1) {
            continue;   /* 超时, 回去再读 */
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
                } else if (strcmp(line, "ldirect") == 0) {
                    /* A/B 对照开关: 直绘 vs 拷贝到槽位再画 */
                    s_lcd_direct_draw = !s_lcd_direct_draw;
                    ESP_LOGI(TAG, "LCD 显示路径: %s",
                             s_lcd_direct_draw
                             ? "直绘 (不拷贝)"
                             : "旧路径 (拷贝到 PSRAM 槽位)");
                } else if (strcmp(line, "noise") == 0) {
                    /* 噪点统计开关 (会读 PSRAM, 排障用) */
                    s_noise_enabled = !s_noise_enabled;
                    ESP_LOGI(TAG, "噪点统计: %s", s_noise_enabled ? "开" : "关");
                } else if (strcmp(line, "stat") == 0) {
                    ESP_LOGI(TAG, "cam_frames=%u jpg_frames=%u jpg_bytes=%u torn=%u clean=%u",
                             (unsigned)s_cam_frames, (unsigned)s_jpg_frames,
                             (unsigned)s_jpg_bytes,
                             (unsigned)s_torn_frames, (unsigned)s_clean_frames);
                } else if (strcmp(line, "cfg") == 0) {
                    camera_print_config();
                } else if (strncmp(line, "bstab ", 6) == 0) {
                    /* bstab 0/1  开关亮度稳定器; bstab 单独则打印状态 */
                    if (strlen(line) > 6) {
                        s_bright_stab = (atoi(line + 6) != 0);
                        ESP_LOGI(TAG, "亮度稳定器: %s", s_bright_stab ? "开" : "关");
                    } else {
                        ESP_LOGI(TAG, "亮度稳定器: %s  参考亮度=%.0f  已调整 %u 帧  "
                                      "最近缩放=%u%%",
                                 s_bright_stab ? "开" : "关",
                                 (double)s_bright_ref,
                                 (unsigned)s_bright_adjust_count,
                                 (unsigned)s_bright_last_scale_pct);
                    }
                } else if (strncmp(line, "bref ", 5) == 0) {
                    int v = atoi(line + 5);
                    if (v < 1000 || v > 60000) {
                        ESP_LOGE(TAG, "用法: bref <1000..60000>");
                    } else {
                        s_bright_ref = (float)v;
                        ESP_LOGI(TAG, "参考亮度设为 %d", v);
                    }
                } else if (strncmp(line, "wr ", 3) == 0) {
                    /* wr <reg> <val>  写 page0 的任意寄存器 (调试用) */
                    unsigned r = 0, v = 0;
                    if (sscanf(line + 3, "%x %x", &r, &v) != 2 || r > 0xff || v > 0xff) {
                        ESP_LOGE(TAG, "用法: wr <reg> <val>   (都是十六进制, 如 wr 22 52)");
                    } else {
                        uint8_t page = 0xfe;
                        uint8_t buf[2] = { (uint8_t)r, (uint8_t)v };
                        if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                                       &page, 1, 100 / portTICK_PERIOD_MS) == ESP_OK &&
                            i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                                       buf, 2, 100 / portTICK_PERIOD_MS) == ESP_OK) {
                            uint8_t rb = 0;
                            i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                                       (uint8_t[]){(uint8_t)r}, 1,
                                                       100 / portTICK_PERIOD_MS);
                            i2c_master_read_from_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                                        &rb, 1, 100 / portTICK_PERIOD_MS);
                            ESP_LOGI(TAG, "写 0x%02x = 0x%02x, 回读 0x%02x", r, v, rb);
                        } else {
                            ESP_LOGE(TAG, "写 0x%02x 失败", r);
                        }
                    }
                } else if (strncmp(line, "aelock ", 7) == 0) {
                    camera_set_ae_lock(atoi(line + 7) != 0);
                } else if (strncmp(line, "expo ", 5) == 0) {
                    int t = 0, g = -1;
                    int n = sscanf(line + 5, "%d %d", &t, &g);
                    if (n < 1) {
                        ESP_LOGE(TAG, "用法: expo <time> [gain]  (time 0..4095, gain 0..63)");
                    } else {
                        if (g < 0) g = 0x14;   /* 默认全局增益 20 */
                        camera_set_manual_exposure(t, g);
                    }
                } else if (strncmp(line, "cgain ", 6) == 0) {
                    int g = atoi(line + 6);
                    if (g < 0 || g > 63) {
                        ESP_LOGE(TAG, "用法: cgain <0..63>");
                    } else {
                        uint8_t page = 0xfe;
                        uint8_t buf[2] = {0x50, (uint8_t)g};
                        if (i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                                       &page, 1, 100 / portTICK_PERIOD_MS) == ESP_OK) {
                            i2c_master_write_to_device(BOARD_I2C_PORT, CAM_SCCB_ADDR,
                                                       buf, 2, 100 / portTICK_PERIOD_MS);
                            ESP_LOGI(TAG, "摄像头全局增益设为 %d (0x%02x)", g, g);
                        }
                    }
                } else if (strncmp(line, "gain ", 5) == 0) {
                    /*
                     * 【命令名冲突 —— 之前踩过的坑】
                     *
                     * 摄像头的全局增益叫 "gain <0..63>", 麦克风增益也叫
                     * "gain <dB>", 两者撞名。摄像头分支在前, 音频分支永远
                     * 执行不到, 结果 "gain 30" 改的是摄像头, 麦克风增益
                     * 还是默认的 12dB —— 查电平不涨就是踩了这个坑。
                     *
                     * 现在拆开: 摄像头用 cgain, 麦克风用 mgain。
                     * "gain" 保留但只提示, 避免再误用。
                     */
                    ESP_LOGW(TAG, "命令名有歧义, 请用:");
                    ESP_LOGW(TAG, "  cgain <0..63>  摄像头全局增益");
                    ESP_LOGW(TAG, "  mgain <-40..40>  麦克风增益 (dB)");
                } else if (strncmp(line, "regs", 4) == 0) {
                    /* regs <st> <en>            -> page0
                     * regs <page> <st> <en>     -> 指定页
                     * (都支持 0x 十六进制) */
                    unsigned pg = 0, st = 0x00, en = 0x5f;
                    int n = sscanf(line + 4, "%x %x %x", &pg, &st, &en);
                    if (n == 3) {
                        if (pg > 0xff) pg = 0;
                        if (st > 0xff) st = 0xff;
                        if (en > 0xff) en = 0xff;
                        if (en < st) en = st;
                        camera_dump_regs_page((int)pg, (int)st, (int)en);
                    } else if (n == 2) {
                        if (st > 0xff) st = 0xff;
                        if (en > 0xff) en = 0xff;
                        if (en < st) en = st;
                        camera_dump_regs_range((int)st, (int)en);
                    } else {
                        camera_dump_regs_range(0x00, 0x5f);
                    }
                } else if (strcmp(line, "q") == 0) {
                    /*
                     * 花屏量化指标。
                     *
                     * 【最重要】g2_bad: 绿色低 3 位的坏点占比。
                     *   干净画面 < 1%; 花屏状态 11~20%。
                     *   这是逐通道算的真指标 (旧的 hi/lo_energy 有位域边界伪像)。
                     *
                     * 旧的 hi/lo_energy 只作参考: 它的低字节能量天然偏大
                     * (跨位域边界会有 32 的台阶), 不能当作噪声判据。
                     */
                    ESP_LOGI(TAG, "QUAL: g2_bad=%u.%02u%% g2_energy=%u.%02u r5_energy=%u.%02u b5_energy=%u.%02u",
                             (unsigned)(s_g2_bad_pct / 100), (unsigned)(s_g2_bad_pct % 100),
                             (unsigned)(s_g2_energy / 100), (unsigned)(s_g2_energy % 100),
                             (unsigned)(s_r5_energy / 100), (unsigned)(s_r5_energy % 100),
                             (unsigned)(s_b5_energy / 100), (unsigned)(s_b5_energy % 100));
                    ESP_LOGI(TAG, "      (旧指标,仅供参考) hi_energy=%u.%02u lo_energy=%u.%02u bad_rows=%u/%u frames=%u",
                             (unsigned)(s_hi_energy / 100), (unsigned)(s_hi_energy % 100),
                             (unsigned)(s_lo_energy / 100), (unsigned)(s_lo_energy % 100),
                             (unsigned)s_bad_rows, (unsigned)NOISE_PROBE_ROWS,
                             (unsigned)s_qual_frames);
                } else if (strcmp(line, "hash") == 0) {
                    /* 触发缓冲稳定性测试 (会阻塞 250ms, 帧率临时掉到 4fps) */
                    s_hash_test_armed = true;
                    ESP_LOGI(TAG, "缓冲稳定性测试已触发, 稍等结果...");
                } else if (strncmp(line, "conv ", 5) == 0) {
                    /*
                     * A/B 切换硬件转换器。
                     *   conv 1 = YUV422 + 硬件转换器
                     *   conv 0 = GC0308 直接输出 RGB565 (不走转换器)
                     *
                     * 【重要】之前这个命令会直接 esp_camera_deinit(),
                     * 而生产者任务正在 esp_camera_fb_get() 里 —— 两者并发,
                     * 立刻 RTC_SW_CPU_RST 复位, 板子重启, 转换器又变回"开",
                     * 所以这个 A/B 从来没真正测成功过。
                     *
                     * 现在: 先让生产者安全退出 (它会在【没握帧】的时候退出),
                     * 再做 deinit/init。
                     */
                    int on = atoi(line + 5);
                    bool want = (on != 0);
                    if (want == s_use_converter) {
                        ESP_LOGI(TAG, "转换器已经是 %s", want ? "开" : "关");
                    } else {
                        ESP_LOGI(TAG, "切换转换器: %s -> %s (安全暂停生产者)",
                                 s_use_converter ? "开" : "关", want ? "开" : "关");
                        /* 1) 请求生产者暂停 */
                        s_cam_pause = true;
                        /* 2) 等它确认已退出 (最多 2 秒) */
                        int waited = 0;
                        while (!s_cam_paused && waited < 200) {
                            vTaskDelay(pdMS_TO_TICKS(10));
                            waited++;
                        }
                        if (!s_cam_paused) {
                            ESP_LOGE(TAG, "生产者没能安全暂停, 放弃切换 (不冒险复位)");
                            s_cam_pause = false;
                        } else {
                            s_use_converter = want;
                            esp_camera_deinit();
                            camera_init();
                            s_cam_pause = false;
                            ESP_LOGI(TAG, "切换完成, 现在发 q 看 g2_bad "
                                     "(干净 <1%%, 花屏 11~20%%)");
                        }
                    }
                } else if (strncmp(line, "mode ", 5) == 0) {
                    /*
                     * 切换传感器输出模式 (决定性 A/B 实验):
                     *   mode 0 = YUV422 + 硬件转换器 -> RGB565 (当前默认)
                     *   mode 1 = 传感器直出 RGB565 (不走转换器)
                     *   mode 2 = GRAYSCALE (1 字节/像素, 不打包不转换)
                     *
                     * mode 2 是最干净的探针: 如果灰度完全干净, 说明 DVP 链路
                     * 本身没问题, 毛病在 "16 位打包 + 转换器" 这一环。
                     */
                    int m = atoi(line + 5);
                    if (m < 0 || m > 3) {
                        ESP_LOGI(TAG, "mode 必须是 0/1/2/3");
                    } else if (m == s_sensor_mode) {
                        ESP_LOGI(TAG, "已经是模式 %d", m);
                    } else {
                        ESP_LOGI(TAG, "切换传感器模式 %d -> %d", s_sensor_mode, m);
                        s_cam_pause = true;
                        int waited = 0;
                        while (!s_cam_paused && waited < 200) {
                            vTaskDelay(pdMS_TO_TICKS(10));
                            waited++;
                        }
                        if (!s_cam_paused) {
                            ESP_LOGE(TAG, "生产者没能安全暂停, 放弃切换");
                            s_cam_pause = false;
                        } else {
                            s_sensor_mode = m;
                            esp_camera_deinit();
                            camera_init();
                            s_cam_pause = false;
                            ESP_LOGI(TAG, "切换完成");
                        }
                    }
                } else if (strcmp(line, "rawdump") == 0) {
                    /*
                     * 通过串口导出一帧原始 RGB565 (取证用)。
                     *
                     * 好处: 不依赖 Wi-Fi/HTTP, 在任何时候都能把原始字节流
                     * 拿回电脑逐位分析。格式:
                     *   RAWDUMP <seq> <len>\r\n<len 字节二进制>\r\nDONE\r\n
                     */
                    s_raw_dump_requested = true;
                    ESP_LOGI(TAG, "已请求原始帧导出, 等待下一帧...");
                } else if (strcmp(line, "gfix") == 0) {
                    /*
                     * 软件修复绿色低位 (toggle)。
                     *
                     * 硬件转换器的绿色低 3 位是随机噪声 (实测自相关 0.39,
                     * 熵 3.00 bit), 而绿色高 3 位是干净的 (自相关 0.95)。
                     * 所以可以用邻居中值把低 3 位填回去:
                     *   修复前 G2 自相关 0.39 -> 修复后 0.92。
                     *
                     * 开启后: 屏幕上绿色闪烁消失, g2_bad 应该掉到 1% 以下。
                     */
                    s_green_fix = !s_green_fix;
                    ESP_LOGI(TAG, "软件修复绿色低位: %s", s_green_fix ? "开" : "关");
                    if (s_green_fix) {
                        ESP_LOGI(TAG, "  (下一帧生效, 稍等 1 秒再发 q 看 g2_bad)");
                    }
                } else if (strncmp(line, "cbar", 4) == 0) {
                    /*
                     * 传感器彩条 A/B —— 判定根因在"采集链路"还是"数据解释"。
                     *
                     *   cbar 1 = 打开彩条 (固定图案, 不经过镜头)
                     *   cbar 0 = 关掉彩条, 恢复正常画面
                     *
                     * 用法: cbar 1 -> 等 2 秒 -> 抓一帧原始数据到电脑看。
                     *       彩条干净 = 链路好, 继续查数据解释;
                     *       彩条也花 = 链路坏, 查 DVP/时钟/DMA。
                     */
                    int on = atoi(line + 5);
                    camera_set_colorbar(on != 0);
                } else if (strncmp(line, "pclk ", 5) == 0) {
                    /*
                     * 运行时调节 PCLK 分频 (GC0308 寄存器 0x28 bit[6:4])。
                     *
                     *   pclk 0 = 不分频 (PCLK ~48MHz, 不可靠)
                     *   pclk 1 = 2 分频 (PCLK ~24MHz)
                     *   pclk 2 = 4 分频 (PCLK ~12MHz)  <- 【当前默认, 最干净】
                     *   pclk 3 = 8 分频 (PCLK ~6MHz,  太慢)
                     *
                     * 分频越大 PCLK 越慢, DVP 采样越可靠, 但帧率也越低。
                     * 客观评估方法: cbar 1 开彩条 -> rawdump 连抓两帧 ->
                     * 逐位对比, bit 翻转率越低越干净。
                     */
                    camera_set_pclk_div(atoi(line + 5));
                } else if (strncmp(line, "spiclk ", 7) == 0) {
                    /*
                     * 运行时调 LCD 的 SPI 时钟 (A/B 对照)。
                     *
                     *   spiclk 60000000 / 40000000 / 20000000 / 10000000
                     *
                     * 背景: 实测证明【摄像头数据是干净的】(浏览器里的 MJPEG
                     * 画面完全没有噪点, JPEG 编码器和 LCD 读的是同一个
                     * fb->buf), 所以花屏只发生在 LCD 的 SPI 发送路径。
                     * 现在双缓冲 + ISR 安全信号量已经修掉了 buf 复用竞争,
                     * 如果还花, 下一个嫌疑就是 SPI 时钟太快导致采样出错。
                     */
                    int hz = atoi(line + 7);
                    if (hz < 1000000 || hz > 80000000) {
                        ESP_LOGE(TAG, "频率要在 1MHz~80MHz 之间");
                    } else {
                        lcd_set_spi_clock(hz);
                    }
                } else if (strcmp(line, "gfixstat") == 0) {
                    ESP_LOGI(TAG, "GFIX: %s", s_green_fix ? "开" : "关");
                } else if (strcmp(line, "wifioff") == 0) {
                    /*
                     * 关掉 Wi-Fi —— 决定性对照实验。
                     *
                     * 假设: 花屏是 DVP 往 PSRAM 写数据时和 Wi-Fi 抢总线导致的
                     *       (esp32-camera README: "image data might be missing.
                     *        This is particularly true if WiFi is enabled.")
                     * 验证: 关掉 Wi-Fi 后再发 "q", 如果 bad_rows 掉到 0, 假设成立。
                     */
                    esp_wifi_stop();
                    ESP_LOGI(TAG, "Wi-Fi 已停止 (现在发 q 看 bad_rows 是否归零)");
                } else if (strncmp(line, "pda ", 4) == 0) {
                    /*
                     * A/B 切换 PSRAM DMA 直写模式。
                     *
                     *   pda 1 = DVP 直写 PSRAM (快, 但会丢 bit -> 花屏)
                     *   pda 0 = DVP 落内部 RAM, CPU 再拷 (慢一点, 但数据可靠)
                     *
                     * 切换会重建整套摄像头 DMA, 需要一两秒。
                     * 切换完发 "q" 看 bad_rows, 用数字决定哪个好。
                     */
                    int on = atoi(line + 4);
                    bool want = (on != 0);
                    if (want == s_psram_dma_mode) {
                        ESP_LOGI(TAG, "PSRAM DMA 模式已经是 %s", want ? "开" : "关");
                    } else {
                        ESP_LOGI(TAG, "切换 PSRAM DMA 模式: %s -> %s",
                                 s_psram_dma_mode ? "开" : "关", want ? "开" : "关");
                        /* 和 conv 一样, 必须先安全暂停生产者 */
                        s_cam_pause = true;
                        int waited = 0;
                        while (!s_cam_paused && waited < 200) {
                            vTaskDelay(pdMS_TO_TICKS(10));
                            waited++;
                        }
                        if (!s_cam_paused) {
                            ESP_LOGE(TAG, "生产者没能安全暂停, 放弃切换");
                            s_cam_pause = false;
                        } else {
                            esp_err_t perr = esp_camera_set_psram_mode(want);
                            if (perr != ESP_OK) {
                                ESP_LOGE(TAG, "切换失败: %s", esp_err_to_name(perr));
                            } else {
                                s_psram_dma_mode = want;
                                ESP_LOGI(TAG, "切换完成, 现在发 q 看 g2_bad");
                            }
                            s_cam_pause = false;
                        }
                    }
                } else if (strcmp(line, "wifix") == 0) {
                    esp_wifi_start();
                    ESP_LOGI(TAG, "Wi-Fi 已重启");
                } else if (strcmp(line, "tone") == 0) {
                    /* toggle: 已经在跑就停, 没在跑就开 */
                    audio_board_stats_t as;
                    audio_board_get_stats(&as);
                    if (as.tone_playing) {
                        audio_board_stop_tone();
                        ESP_LOGI(TAG, "测试音已停止");
                    } else if (audio_board_start_tone() == ESP_OK) {
                        ESP_LOGI(TAG, "测试音已启动 (3 秒一轮, 再发 tone 停止)");
                    } else {
                        ESP_LOGE(TAG, "测试音启动失败");
                    }
                } else if (strcmp(line, "axtest") == 0) {
                    audio_selftest_loop();
                } else if (strcmp(line, "scan") == 0) {
                    wifi_scan_and_report();
                } else if (strcmp(line, "wifishow") == 0) {
                    /*
                     * 看当前生效的 WiFi 配置。
                     *
                     * 密码不打印 —— 串口日志可能被录下来存进文档。
                     */
                    ESP_LOGI(TAG, "WiFi 配置: SSID=\"%s\" 认证=%s 来源=%s",
                             wifi_config_ssid(),
                             wifi_config_pass()[0] ? "WPA/WPA2" : "OPEN",
                             wifi_config_from_nvs() ? "NVS (串口设过)" : "编译期默认值");
                    ESP_LOGI(TAG, "  提示: wifiset <SSID> [密码]  改配置并重连");
                    ESP_LOGI(TAG, "        wififorgot           忘掉 NVS 配置, 用默认值");
                } else if (strncmp(line, "wifiset ", 8) == 0 ||
                           strncmp(line, "wifisave ", 9) == 0) {
                    /*
                     * 设置 WiFi 配置并存进 NVS —— 换网不用重新烧固件。
                     *
                     *   wifiset <SSID> [密码]   设置 + 立即重连
                     *   wifisave <SSID> [密码]  只保存, 不重连
                     *
                     * 密码省略 = 开放网络 (校园网常见)。
                     * SSID 可以带空格, 所以从第一个空格之后全部当作 SSID,
                     * 再往后的空格算密码分隔。
                     */
                    bool do_reconnect = (line[7] == 't');   /* "wifiset" 的第 8 个字符是 't' */
                    const char *p = (line[7] == 't') ? line + 8 : line + 9;
                    while (*p == ' ') p++;

                    /* 拆成 SSID 和密码: 第一个空格分隔 */
                    char ssid[64] = {0};
                    char pass[80] = {0};
                    const char *sp = strchr(p, ' ');
                    if (sp) {
                        size_t n = (size_t)(sp - p);
                        if (n >= sizeof(ssid)) n = sizeof(ssid) - 1;
                        memcpy(ssid, p, n);
                        ssid[n] = '\0';
                        const char *q = sp + 1;
                        while (*q == ' ') q++;
                        strncpy(pass, q, sizeof(pass) - 1);
                    } else {
                        strncpy(ssid, p, sizeof(ssid) - 1);
                    }

                    if (ssid[0] == '\0') {
                        ESP_LOGE(TAG, "用法: wifiset <SSID> [密码]");
                    } else {
                        esp_err_t r = wifi_config_save(ssid, pass);
                        if (r != ESP_OK) {
                            ESP_LOGE(TAG, "保存失败: %s", esp_err_to_name(r));
                        } else {
                            ESP_LOGI(TAG, "已保存 SSID=\"%s\" (%s)",
                                     ssid, pass[0] ? "有密码" : "开放网络");
                            if (do_reconnect) {
                                ESP_LOGI(TAG, "正在用新配置重连...");
                                /*
                                 * 不 esp_wifi_disconnect() —— 那个会触发
                                 * WIFI_EVENT_STA_DISCONNECTED, 走重连退避逻辑。
                                 * 直接 set_config + connect 最直接。
                                 */
                                esp_wifi_disconnect();
                                wifi_config_t cfg = {0};
                                strncpy((char *)cfg.sta.ssid, ssid, sizeof(cfg.sta.ssid) - 1);
                                if (pass[0]) {
                                    strncpy((char *)cfg.sta.password, pass,
                                            sizeof(cfg.sta.password) - 1);
                                    cfg.sta.threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK;
                                } else {
                                    cfg.sta.threshold.authmode = WIFI_AUTH_OPEN;
                                }                                cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
                                esp_wifi_set_config(WIFI_IF_STA, &cfg);
                                esp_wifi_connect();
                            }
                        }
                    }
                } else if (strcmp(line, "wififorgot") == 0) {
                    esp_err_t r = wifi_config_forget();
                    if (r == ESP_OK) {
                        ESP_LOGI(TAG, "已清除 NVS 配置, 重启后用编译期默认值 (SSID=\"%s\")",
                                 WIFI_SSID);
                    }
                } else if (strcmp(line, "memstat") == 0) {
                    /*
                     * 内存分区统计 —— 定位"内部 RAM 被谁吃了"的主力工具。
                     *
                     * heap_caps_get_allocated_size 返回的是【已分配】的字节数,
                     * 减一下总量就知道还剩多少。
                     */
                    const uint32_t cap_names[] = {
                        MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT,
                        MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL,
                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT,
                    };
                    const char *cap_labels[] = {"内部RAM(8bit)", "内部RAM(DMA)", "PSRAM(8bit)"};
                    for (int i = 0; i < 3; i++) {
                        size_t total = heap_caps_get_total_size(cap_names[i]);
                        size_t free  = heap_caps_get_free_size(cap_names[i]);
                        size_t largest = heap_caps_get_largest_free_block(cap_names[i]);
                        /* IDF 没有"按分区查已用"的接口, 用总量减剩余近似。
                         * (碎片化会让这个数偏大, 够用了) */
                        size_t used = (total > free) ? (total - free) : 0;
                        ESP_LOGI(TAG, "[内存] %-14s 总量=%" PRIu32 " 已用=%" PRIu32
                                 " 剩余=%" PRIu32 " 最大块=%" PRIu32,
                                 cap_labels[i], (uint32_t)total, (uint32_t)used,
                                 (uint32_t)free, (uint32_t)largest);
                    }
                    ESP_LOGI(TAG, "[内存] 任务数=%u  当前空闲栈=%u",
                             (unsigned)uxTaskGetNumberOfTasks(),
                             (unsigned)uxTaskGetStackHighWaterMark(NULL));
                } else if (strcmp(line, "astat") == 0) {
                    audio_board_stats_t as;
                    if (audio_board_get_stats(&as) == 0) {
                        ESP_LOGI(TAG, "AUDIO peak=%u rms=%u L(p=%u r=%u) samples=%u frames=%u speaking=%d tone=%d",
                                 (unsigned)as.peak, (unsigned)as.rms,
                                 (unsigned)as.peak_l, (unsigned)as.peak_r,
                                 (unsigned)as.samples, (unsigned)as.frames,
                                 (int)as.speaking, (int)as.tone_playing);
                    }
                } else if (strcmp(line, "astop") == 0) {
                    audio_board_stop_background();
                    ESP_LOGI(TAG, "音频后台任务已停止");
                } else if (strncmp(line, "vol ", 4) == 0) {
                    int v = atoi(line + 4);
                    if (v < 0) v = 0;
                    if (v > 100) v = 100;
                    if (audio_board_set_output_volume(v) == 0) {
                        ESP_LOGI(TAG, "输出音量 = %d", v);
                    } else {
                        ESP_LOGE(TAG, "设置音量失败");
                    }
                } else if (strncmp(line, "mgain ", 6) == 0) {
                    float g = (float)atof(line + 6);
                    if (g < -40.0f) g = -40.0f;
                    if (g > 40.0f)  g = 40.0f;
                    if (audio_board_set_input_gain(g) == 0) {
                        ESP_LOGI(TAG, "麦克风增益 = %.1f dB", g);
                    } else {
                        ESP_LOGE(TAG, "设置增益失败");
                    }
                } else if (strcmp(line, "adcreg") == 0) {
                    audio_board_dump_adc_regs();
                } else if (strncmp(line, "evtest", 5) == 0) {
                    /*
                     * 注入一条看护事件 (M2 链路自测)。
                     *
                     * evtest              -> 用默认文案 (BUTTON_MESSAGE)
                     * evtest <自定义文案> -> 用自定义文案 (验证 A6 文案可配置)
                     *
                     * 注意: 串口命令行的分隔符是【空格】, 不是 tab,
                     * 所以要用 strchr 找第一个空格, 不能假设是 line[5]。
                     */
                    const char *msg = BUTTON_MESSAGE;
                    const char *sp = strchr(line, ' ');
                    if (sp != NULL && sp[1] != '\0') {
                        msg = sp + 1;
                    }
                    inject_button_event(msg);
                } else if (strncmp(line, "lcdprompt", 8) == 0) {
                    /*
                     * 屏幕提示自测 (需求文档 §3.8)。
                     *
                     *   lcdprompt                    -> 列出所有类型
                     *   lcdprompt <类型> [文案]      -> 显示对应提示
                     *
                     * 类型: help / cancel / watching / calling / content
                     */
                    const char *sp = strchr(line, ' ');
                    const char *arg = (sp && sp[1]) ? sp + 1 : "";
                    const char *arg2 = (sp && strchr(sp + 1, ' ')) ? strchr(sp + 1, ' ') + 1 : "";

                    if (arg[0] == '\0') {
                        ESP_LOGI(TAG, "用法: lcdprompt <help|cancel|watching|calling|content> [文案]");
                        ESP_LOGI(TAG, "  help     - 已通知家人 (10s)");
                        ESP_LOGI(TAG, "  cancel   - 求助已取消 (5s)");
                        ESP_LOGI(TAG, "  watching - 家人正在看画面 (5s)");
                        ESP_LOGI(TAG, "  calling  - 家人正在呼叫您 (8s, 伴随提示音)");
                        ESP_LOGI(TAG, "  content  - 干预内容 (10s, 可带自定义文案)");
                    } else if (strcmp(arg, "help") == 0) {
                        lcd_prompt_show(PROMPT_HELP_SENT, NULL, 0);
                    } else if (strcmp(arg, "cancel") == 0) {
                        lcd_prompt_show(PROMPT_HELP_CANCELED, NULL, 0);
                    } else if (strcmp(arg, "watching") == 0) {
                        lcd_prompt_show(PROMPT_WATCHING, NULL, 0);
                    } else if (strcmp(arg, "calling") == 0) {
                        lcd_prompt_show(PROMPT_CALLING, NULL, 0);
                        audio_board_play_prompt_beep();
                    } else if (strcmp(arg, "content") == 0) {
                        lcd_prompt_show(PROMPT_CONTENT, arg2[0] ? arg2 : NULL, 0);
                    } else {
                        ESP_LOGE(TAG, "未知类型: %s", arg);
                    }
                } else if (strncmp(line, "actest", 6) == 0) {
                    /*
                     * 活动检测自测 (需求文档 §3.5)。
                     *
                     *   actest          打印当前活动状态 (JSON)
                     *   actest stats    打印详细统计
                     */
                    char jbuf[512];
                    if (activity_detect_to_json(jbuf, sizeof(jbuf)) > 0) {
                        ESP_LOGI(TAG, "ACTIVITY %s", jbuf);
                    } else {
                        ESP_LOGE(TAG, "活动检测未初始化");
                    }
                    const activity_state_t *st = activity_detect_get_state();
                    ESP_LOGI(TAG, "  分析帧数=%" PRIu32 " 状态变化=%" PRIu32
                             " 连续有信号=%" PRIu32 " 平均亮度=%d",
                             (uint32_t)st->analyze_count,
                             (uint32_t)st->state_change_count,
                             (uint32_t)st->active_streak,
                             (int)st->avg_luma);
                    ESP_LOGI(TAG, "  运动=%d(%.3f) 人形=%d(%.2f) 低光=%d 检测器=%d",
                             (int)st->motion, (double)st->motion_ratio,
                             (int)st->human_present, (double)st->human_score,
                             (int)st->low_light,
                             st->has_detector ? 1 : 0);
                } else if (strncmp(line, "btnraw", 6) == 0) {
                    /*
                     * 打印按键原始电平 (排障用)。
                     *   btnraw       打印一次
                     *   btnraw 30    连续打印 30 秒
                     */
                    int secs = (strlen(line) > 6) ? atoi(line + 6) : 1;
                    button_raw_dump(secs);
                } else if (strncmp(line, "awregs", 6) == 0) {
                    aw9523b_dump_regs();
                } else if (strcmp(line, "bat") == 0) {
                    /*
                     * 打印电池 / 供电状态。
                     *
                     * 这是回答"板子上到底有没有装电池"的直接手段:
                     *   - 电压落在 3.4V~4.2V -> 电池装好且有电
                     *   - 读到 0V 或满量程 4095 -> 电池座空 / 接触不良 / 没有电池电路
                     *
                     * 注意: 内部有 500ms 阻塞延时 (等分压稳定)。
                     */
                    {
                        power_status_t st = power_board_get_status();
                        int adc = 0, mv = 0, pct = 0;
                        if (power_board_read_battery(&adc, &mv, &pct) == ESP_OK) {
                            /* valid = 读到了可信的电池电压 (按 ADC 原始码判定) */
                            st.valid   = (adc >= BOARD_BAT_VALID_MIN_ADC);
                            st.adc_raw = adc;
                            st.mv      = mv;
                            st.percent = pct;
                            st.charging = power_board_is_charging();
                            /* 用本轮读到的电压做交叉验证, 避免用到过期缓存 */
                            st.source   = power_board_determine_source(&st);
                        } else {
                            st.valid = false;
                        }

                        char line_buf[160];
                        power_board_format_status(&st, line_buf, sizeof(line_buf));
                        ESP_LOGI(TAG, "电池: %s", line_buf);

                        /*
                         * 判读提示 —— 直接告诉用户"有没有电池"。
                         *
                         * 关键判据是【ADC 原始码】, 而不是换算后的电压:
                         *   2696~3280  -> 有效电池电压区间 (官方分档表)
                         *   4095       -> 满量程, ADC 没切到电池通道 / 分压上端悬空
                         *   0          -> 输入被拉到地
                         *   其他偏小值  -> 悬空引脚的杂散电压 (实测 250~310)
                         */
                        if (st.adc_raw >= 4090) {
                            ESP_LOGW(TAG, "  => 满量程 (4095): 分压回路断开, "
                                          "大概率没装电池或底板不带电池电路");
                        } else if (st.adc_raw == 0) {
                            ESP_LOGW(TAG, "  => 0V: 电池座空 / 接触不良 / 无电池电路");
                        } else if (st.adc_raw >= 2400) {
                            ESP_LOGI(TAG, "  => 电池已装且有电 (正常读数)");
                        } else {
                            ESP_LOGW(TAG, "  => ADC=%d 偏低 (有效区间是 2696~3280): "
                                          "分压回路没接上, 大概率没装电池",
                                     st.adc_raw);
                        }
                    }
                } else if (strcmp(line, "batdiag") == 0) {
                    /*
                     * 电池电路深度诊断 —— 扫描各种控制脚组合。
                     * 用来区分"没装电池"和"我们控制脚用错了"。
                     */
                    power_board_diagnose();
                } else if (strncmp(line, "ai", 2) == 0 &&
                           (line[2] == '\0' || line[2] == ' ')) {
                    /*
                     * AI 对话调试。
                     *
                     *   ai        打印状态 (连接/轮次/最近一轮对话内容)
                     *   ai go     手动发起一轮对话 (不依赖唤醒词)
                     *
                     * 为什么要有 "ai go":
                     *   唤醒词 (esp-sr) 一旦不准, 就没法验证"采集->上传->播放"
                     *   这条链路到底通没通。手动触发把【唤醒】这一环摘掉,
                     *   剩下的环节能独立验证 —— 排障时非常关键。
                     */
                    const char *cmd = line + 2;
                    while (*cmd == ' ') cmd++;

                    if (strcmp(cmd, "go") == 0) {
                        ESP_LOGI(TAG, "手动发起一轮 AI 对话...");
                        esp_err_t r = ai_client_start_turn();
                        if (r != ESP_OK) {
                            ESP_LOGE(TAG, "发起失败: %s", esp_err_to_name(r));
                        }
                    } else {
                        ai_client_stats_t s;
                        ai_client_get_stats(&s);
                        ESP_LOGI(TAG, "AI 状态: %s  连接=%s",
                                 ai_client_state_str(),
                                 s.connected ? "是" : "否");
                        ESP_LOGI(TAG, "AI 统计: 轮次=%u 回复=%u 错误=%u 连接失败=%u",
                                 (unsigned)s.turns, (unsigned)s.replies,
                                 (unsigned)s.errors, (unsigned)s.connect_fails);
                        ESP_LOGI(TAG, "AI 最近识别: %s", s.last_user);
                        ESP_LOGI(TAG, "AI 最近回复: %s", s.last_reply);
                    }
                } else if (strcmp(line, "wake") == 0) {
                    /*
                     * 唤醒词调试。
                     *
                     *   wake              打印状态 (就绪/命中次数/概率/门限)
                     *   wake go           手动触发一次唤醒 (不依赖模型识别)
                     *   wake threshold 0.6 现场调检测门限 (0.0~0.9999)
                     */
                    const char *cmd = line + 4;
                    while (*cmd == ' ') cmd++;

                    if (strncmp(cmd, "threshold", 9) == 0) {
                        const char *arg = cmd + 9;
                        while (*arg == ' ') arg++;
                        if (*arg == '\0') {
                            ESP_LOGI(TAG, "当前门限: %.2f", wake_word_last_prob());
                        } else {
                            float t = atof(arg);
                            esp_err_t r = wake_word_set_threshold(t);
                            if (r != ESP_OK) {
                                ESP_LOGE(TAG, "门限设置失败: %s", esp_err_to_name(r));
                            }
                        }
                    } else if (strcmp(cmd, "go") == 0) {
                        ESP_LOGI(TAG, "手动触发唤醒...");
                        esp_err_t r = wake_word_trigger_manual();
                        if (r != ESP_OK) {
                            ESP_LOGE(TAG, "触发失败: %s", esp_err_to_name(r));
                        }
                    } else {
                        wake_word_dump_status();
                    }
                } else if (strcmp(line, "voice") == 0 ||
                           strncmp(line, "voice ", 6) == 0) {
                    /*
                     * 语音模式 (一套固件两种用法)。
                     * 命令名不能用 "mode" —— 那个已被摄像头传感器模式占用。
                     *
                     *   voice            打印当前模式
                     *   voice xiaozhi    手动切到小智聊天
                     *   voice care       手动切回看护对讲
                     *
                     * 平时不用敲 —— 喊「小康同学」/「晴老师」自动切。
                     */
                    const char *cmd = line + 5;
                    while (*cmd == ' ') cmd++;

                    if (strcmp(cmd, "xiaozhi") == 0 || strcmp(cmd, "x") == 0) {
                        voice_mode_set(VOICE_MODE_XIAOZHI);
                        ESP_LOGI(TAG, "已切换到小智模式");
                    } else if (strcmp(cmd, "care") == 0 || strcmp(cmd, "c") == 0) {
                        voice_mode_set(VOICE_MODE_CARE);
                        ESP_LOGI(TAG, "已切回看护对讲模式");
                    } else {
                        voice_mode_dump_status();
                    }
                } else if (strcmp(line, "ispeed") == 0) {
                    i2c_speed_probe();
                } else if (strcmp(line, "iprobe") == 0) {
                    /* 运行期跑一次 I2C 引脚物理探针, 与开机时的结果对照 */
                    i2c_pin_physical_probe();
                } else if (strcmp(line, "i2cdump") == 0) {
                    /* 打印 I2C 总线【真实】电平 + 逐地址扫描 */
                    {
                        int sda = 0, scl = 0;
                        board_i2c_real_levels(&sda, &scl);
                        ESP_LOGW(TAG, "I2C 真实电平: SDA=%d SCL=%d", sda, scl);
                    }
                    i2c_scan_audio();
                } else if (strcmp(line, "i2crecover") == 0) {
                    /* 手动触发一次 I2C 总线恢复序列 */
                    board_i2c_bus_recover();
                } else if (strcmp(line, "awinit") == 0) {
                    /* 手动重跑电源/背光/摄像头复位时序 (不开新摄像头) */
                    camera_power_reset();
                } else if (strncmp(line, "keytest", 7) == 0) {
                    aw_key_line_test();
                } else if (strncmp(line, "bootpress", 9) == 0) {
                    boot_button_simulate();
                } else {
                    ESP_LOGW(TAG, "未知命令: %s", line);
                    ESP_LOGW(TAG, "可用: lcd0/lcd1/lcd2/stat/q/hash/conv 0|1/pda 0|1/wifioff/wifix/astat/adcreg/tone/astop/vol N/mgain N/cgain N/scan/axtest/actest/lcdprompt/evtest/bat/batdiag/awregs");
                }
                len = 0;
            }
            continue;
        }
        /*
         * 接受 ASCII 可见字符 + UTF-8 多字节序列 (>= 0x80)。
         *
         * 原来只收 32..126, 结果中文 (UTF-8 每个字 3 字节, 首字节 >= 0xE0)
         * 被整段丢弃 —— 表现为 "evtest 奶奶说想喝水了" 传进去变成了
         * "evtest "，自定义文案根本没生效。
         *
         * 这里只做"收下字节", 不做 UTF-8 合法性校验:
         * 命令解析只按 ASCII 关键字匹配, 多字节内容整体透传,
         * 最后由 JSON 输出和 UTF-8 终端去处理。
         */
        if ((ch >= 32 && ch < 127) || ch >= 0x80) {
            if (len < (int)sizeof(line) - 1) {
                line[len++] = (char)ch;
            }
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
/* 读单个 AW 键的状态, 返回 true = 按下 (低有效) */
static bool aw_key_state(uint8_t pin)
{
    bool high = true;
    if (aw9523b_get_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, pin, &high) == ESP_OK) {
        return !high;
    }
    return false;
}

/*
 * 【注入一个看护事件】—— 排障/自测用。
 *
 * 为什么需要:
 *   M2 的验收标准是"按板子按钮, 网页弹出中文提示", 但这条链路有三个环节:
 *     物理按键 -> GPIO 读取 -> 事件队列 -> /event 出队 -> 网页弹窗
 *   每次验证都要人去按一下板子, 慢且不可重复。
 *   而"网页到底能不能弹出来"这一段, 完全可以离线验证:
 *   只要事件能进队列, /event 就能取出来, 网页就能弹。
 *
 * 所以提供一个"假装按了一下键"的命令, 把按键以外的两个环节独立验证。
 * 物理按键那一段用 tools_m2_check.py 单独验 (或者直接看串口日志)。
 *
 * 用法:
 *   evtest              注入一条默认文案的事件
 *   evtest <自定义文案>  注入一条自定义文案的事件 (验证文案可配置)
 */
/*
 * 把一个看护事件同时送进推模式队列 (push_client)。
 *
 * 前置声明: 电源状态变化回调要用它。
 */
static void push_event_to_cloud(uint32_t seq, uint64_t ts_ms,
                                const char *type, const char *msg);

/*
 * 【电源状态变化回调】—— 由 power_board 的后台监测任务调用。
 *
 * 触发时机:
 *   - Type-C 插上 / 拔掉 (供电来源变化)
 *   - 开始充电 / 停止充电
 *
 * 为什么值得单独推一条事件:
 *   PRD R-05 "设备离线 > 10min" 告警的前提是【知道设备本来在线】。
 *   如果设备被人拔了电, 那和"断网"是两回事, 子女端的处置完全不同
 *   (断网 -> 检查路由器; 断电 -> 检查插座/电池)。这条事件就是给
 *   后台区分这两者的依据。
 */
static void power_status_changed(const power_status_t *now,
                                 const power_status_t *old)
{
    char msg[160];
    uint64_t ts = (uint64_t)(esp_timer_get_time() / 1000);

    const char *type = "power_source_changed";
    if (old->charging != now->charging) {
        type = now->charging ? "charging_started" : "charging_stopped";
    }

    snprintf(msg, sizeof(msg),
             "供电方式%s, 充电%s, 电量%d%%",
             (now->source == POWER_SOURCE_BATTERY) ? "切换为电池" : "切换为Type-C",
             now->charging ? "中" : "停止",
             now->percent);

    ESP_LOGI(TAG, "电源状态变化: %s (%s)", type, msg);

    push_event_to_cloud((uint32_t)ts, ts, type, msg);
}

/*
 * 把一个看护事件同时送进推模式队列 (push_client)。
 *
 * 为什么事件要走【两条路】:
 *   1. s_event_queue —— 拉模式。浏览器轮询 GET /event 取走。
 *      这是可行性验证阶段唯一的通道, 还在用。
 *   2. push_client  —— 推模式。设备主动 TCP 连到电脑推出去。
 *      这是需求文档 §3.9 的要求, 断网时会缓存, 恢复后补传。
 *
 * 两条路【互不抢事件】: 队列里的事件被浏览器取走了, 推模式那份还在。
 * 这不是 bug —— 电脑端 recv_event.py 按 seq 去重, 收到重复的会忽略。
 * 好处是: 网页开着也能让后台服务同时收到事件。
 *
 * seq 用【事件发生时刻】的毫秒时间戳, 不是自增序号 ——
 * 这样两条路上的同一个事件 seq 相同, 电脑端能准确去重。
 */
static void push_event_to_cloud(uint32_t seq, uint64_t ts_ms,
                                const char *type, const char *msg)
{
    char body[320];
    int n = snprintf(body, sizeof(body),
                     "\"type\":\"%s\",\"msg\":\"%s\",\"seq\":%u,\"ts\":%llu",
                     type ? type : "event",
                     msg ? msg : "",
                     (unsigned)seq,
                     (unsigned long long)ts_ms);
    if (n > 0 && n < (int)sizeof(body)) {
        push_client_submit_event(body);
    }
}

static void inject_button_event(const char *msg)
{
    button_event_t ev;
    memset(&ev, 0, sizeof(ev));
    ev.has_event = true;
    ev.seq = (uint32_t)(esp_timer_get_time() / 1000);
    ev.ts_ms = esp_timer_get_time() / 1000;
    snprintf(ev.message, sizeof(ev.message), "%s", msg);

    if (xQueueSend(s_event_queue, &ev, 0) != pdTRUE) {
        ESP_LOGE(TAG, "注入事件失败: 队列已满");
        return;
    }
    ESP_LOGI(TAG, "已注入按键事件: %s", ev.message);

    /* §3.9: 同时进推模式队列, 断网时缓存, 恢复后补传 */
    push_event_to_cloud(ev.seq, ev.ts_ms, "button", ev.message);
}

/*
 * 【按键原始电平诊断】—— 排障用。
 *
 * 为什么需要:
 *   "按了键没反应"这个问题, 光看事件日志只能看到"没产生事件",
 *   不知道是【按键没被读到】还是【读到了但没生成事件】。
 *   直接把三个键的原始电平打出来, 一眼就能定位:
 *     - 电平一直是 1  -> 硬件/上拉/引脚配置问题 (按键根本没接通)
 *     - 电平变 0 但没事件 -> 消抖或事件生成逻辑的问题
 *
 * 用法:
 *   btnraw           打印一次当前电平
 *   btnraw 30        连续打印 30 秒 (每秒一次), 便于按住键观察跳变
 */
static void button_raw_dump(int seconds)
{
    if (seconds <= 0) {
        seconds = 1;
    }
    ESP_LOGI(TAG, "按键原始电平 (BOOT=GPIO%d 高=松开 低=按下)", BTN_BOOT_GPIO);
    for (int i = 0; i < seconds; i++) {
        int boot_lv = gpio_get_level(BTN_BOOT_GPIO);
        int k1_lv = aw_key_state(XIO_KEY_K1) ? 0 : 1;
        int k2_lv = aw_key_state(XIO_KEY_K2) ? 0 : 1;
        ESP_LOGI(TAG, "  [%2d] BOOT=%d K1=%d K2=%d %s",
                 i, boot_lv, k1_lv, k2_lv,
                 (boot_lv == 0 || k1_lv == 0 || k2_lv == 0) ? "<== 有键按下" : "");
        if (i + 1 < seconds) {
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
}

/*
 * 【AW9523B 原始寄存器快照】—— 排障用。
 *
 * 用途: 当按键电平出现"恒定按下"这种怪现象时, 光看应用层的电平不够,
 * 必须把芯片的原始寄存器摊开看, 才能分清是:
 *   - CONFIG 方向位配错了 (该输入的脚被配成输出)
 *   - INPUT 寄存器读回的值本身就不对 (硬件上拉/外部电路问题)
 *   - 读 I2C 失败时上层拿到的是"假的高电平"
 */
static void aw9523b_dump_regs(void)
{
    uint8_t in0 = 0, in1 = 0, out0 = 0, out1 = 0, cfg0 = 0, cfg1 = 0, id = 0;

    if (aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_INPUT_P0, &in0) != ESP_OK ||
        aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_INPUT_P1, &in1) != ESP_OK ||
        aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_OUTPUT_P0, &out0) != ESP_OK ||
        aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_OUTPUT_P1, &out1) != ESP_OK ||
        aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_CONFIG_P0, &cfg0) != ESP_OK ||
        aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_CONFIG_P1, &cfg1) != ESP_OK ||
        aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_ID, &id) != ESP_OK) {
        ESP_LOGE(TAG, "AW9523B 寄存器读取失败 (I2C 通信异常)");
        return;
    }

    ESP_LOGI(TAG, "AW9523B 寄存器: ID=0x%02x", id);
    ESP_LOGI(TAG, "  INPUT   P0=0x%02x P1=0x%02x   (1=高电平)", in0, in1);
    ESP_LOGI(TAG, "  OUTPUT  P0=0x%02x P1=0x%02x", out0, out1);
    ESP_LOGI(TAG, "  CONFIG  P0=0x%02x P1=0x%02x   (1=输入 0=输出)", cfg0, cfg1);
    ESP_LOGI(TAG, "  K1(XIO0) 输入电平=%d, K2(XIO1) 输入电平=%d",
             (in0 & 0x01) ? 1 : 0, (in0 & 0x02) ? 1 : 0);

    uint8_t ctl = 0;
    if (aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_CTL, &ctl) == ESP_OK) {
        ESP_LOGI(TAG, "  CTL(0x11)=0x%02x  P0驱动模式=%s", ctl,
                 (ctl & AW9523B_CTL_GPOMD_BIT) ? "推挽" : "开漏");
    }
    uint8_t led0 = 0, led1 = 0;
    if (aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, 0x12, &led0) == ESP_OK &&
        aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, 0x13, &led1) == ESP_OK) {
        ESP_LOGI(TAG, "  LED模式 P0=0x%02x P1=0x%02x  (1=GPIO 0=LED)", led0, led1);
        ESP_LOGI(TAG, "  K1(bit0)=%s, K2(bit1)=%s  <== 若为 LED 模式则该脚被恒定拉低!",
                 (led0 & 0x01) ? "GPIO" : "LED",
                 (led0 & 0x02) ? "GPIO" : "LED");
    }
    ESP_LOGI(TAG, "  期望: K1/K2 都是输入 (CONFIG P0 bit0/bit1 = 1), 输入电平均为 1 (松开)");
}

/*
 * 【按键脚硬连线测试】—— 排障用。
 *
 * 背景:
 *   实测 XIO1 (K2) 的输入电平恒为 0, 而 XIO0 (K1) 恒为 1。
 *   AW9523B 没有内部上拉寄存器, 所以 K2 读低只有两种可能:
 *     a) 这个脚被硬短接到 GND (按键卡死 / 板上焊接问题)
 *     b) 这个脚没有外部上拉, 悬空时被读成低
 *
 * 怎么区分:
 *   把该脚临时配成【输出推挽】并驱动高电平, 然后回读 INPUT 寄存器。
 *   - 读回 1  -> 脚没有被短到 GND, 只是缺上拉 (情况 b)
 *   - 读回 0  -> 脚被硬短到 GND (情况 a), 按键/硬件坏了
 *
 * 注意:
 *   - 测试期间【不要按键】。推挽输出高电平时如果按键按下, 会把输出直接
 *     短到 GND, 对芯片不好。
 *   - 测试完立刻恢复成输入模式。
 */
static void aw_key_line_test(void)
{
    const uint8_t pins[2] = { XIO_KEY_K1, XIO_KEY_K2 };
    const char *names[2] = { "K1(XIO0)", "K2(XIO1)" };

    for (int i = 0; i < 2; i++) {
        uint8_t pin = pins[i];

        /* 保存当前配置, 测完还原 */
        uint8_t cfg0 = 0, cfg1 = 0;
        aw9523b_get_config(BOARD_I2C_PORT, AW9523B_I2C_ADDR, &cfg0, &cfg1);

        /* 配成输出推挽, 驱动高电平 */
        aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, pin, true);
        aw9523b_set_level(BOARD_I2C_PORT, AW9523B_I2C_ADDR, pin, true);
        vTaskDelay(pdMS_TO_TICKS(20));

        uint8_t in0 = 0;
        if (aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_INPUT_P0, &in0) == ESP_OK) {
            int lvl = (in0 & (1u << (pin & 0x07))) ? 1 : 0;
            ESP_LOGI(TAG, "  %s 驱动高电平后回读=%d  => %s", names[i], lvl,
                     lvl ? "没有硬短到 GND (只是缺上拉)" : "被硬短到 GND (按键/硬件故障)");
        } else {
            ESP_LOGE(TAG, "  %s 回读失败", names[i]);
        }

        /* 还原成输入 */
        aw9523b_set_output(BOARD_I2C_PORT, AW9523B_I2C_ADDR, pin, false);
        vTaskDelay(pdMS_TO_TICKS(20));

        /* 还原后再读一次, 看看是不是真的没上拉 */
        in0 = 0;
        if (aw9523b_read_raw(BOARD_I2C_PORT, AW9523B_I2C_ADDR, AW9523B_REG_INPUT_P0, &in0) == ESP_OK) {
            ESP_LOGI(TAG, "  %s 还原成输入后回读=%d", names[i],
                     (in0 & (1u << (pin & 0x07))) ? 1 : 0);
        }
    }
    ESP_LOGI(TAG, "  (测试期间请不要按键)");
}

/*
 * 【模拟按 BOOT 键】—— 排障/自测用。
 *
 * 背景:
 *   M2 的验收标准是"按板子按钮, 网页弹出中文提示", 但每次都要人工去按,
 *   慢且不可重复。而"BOOT 电平变低 -> 生成事件 -> 网页弹出"这一整条链路
 *   除了最后那一下手指, 全部是软件, 可以自己闭环验证。
 *
 * 做法:
 *   1. 把 GPIO0 临时配成【输出】, 先驱动高电平 (安全, 不会误进下载模式)
 *   2. 拉低 100ms —— 这模拟"按下"
 *   3. 立刻恢复成【输入 + 上拉】—— 这模拟"松开"
 *
 * 【重要】为什么必须先驱动高再拉低:
 *   GPIO0 是 strapping 引脚, 上电瞬间为低会进下载模式。
 *   虽然系统启动后不会因此复位, 但"瞬间拉低"这种写法在某些时序下
 *   会被硬件复位电路捕捉到, 表现为设备莫名其妙重启。
 *   所以先输出高 (100ms), 确认稳定后再拉低。
 */
static void boot_button_simulate(void)
{
    gpio_config_t out_cfg = {
        .pin_bit_mask = (1ULL << BTN_BOOT_GPIO),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    ESP_LOGI(TAG, "模拟按下 BOOT 键 (GPIO%d)", BTN_BOOT_GPIO);

    /* 先驱动高, 稳定 100ms, 再拉低 —— 避免 strapping 引脚瞬间拉低导致复位 */
    gpio_config(&out_cfg);
    gpio_set_level(BTN_BOOT_GPIO, 1);
    vTaskDelay(pdMS_TO_TICKS(100));
    gpio_set_level(BTN_BOOT_GPIO, 0);
    vTaskDelay(pdMS_TO_TICKS(150));
    gpio_set_level(BTN_BOOT_GPIO, 1);
    vTaskDelay(pdMS_TO_TICKS(100));

    /* 恢复成输入上拉, 与真实状态一致 */
    gpio_config_t in_cfg = {
        .pin_bit_mask = (1ULL << BTN_BOOT_GPIO),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&in_cfg);

    ESP_LOGI(TAG, "已恢复 BOOT 键为输入上拉");
}

/*
 * 按键任务。
 *
 * 三个键各有分工:
 *   BOOT (GPIO0)  -> 触发看护事件 (网页弹提示)
 *   K1  (XIO0)    -> 音量 -
 *   K2  (XIO1)    -> 音量 +
 *
 * 音量调节是老人自己最需要的操作 —— 老人耳朵不好, 音量必须能自己调,
 * 不能每次都找子女帮忙改固件。
 *
 * 注意: K1/K2 的方向 (输入) 已在 camera_power_reset() 里设好, 这里不再重复写。
 * 每 50ms 轮询一次, 如果每次都做「读 CONFIG + 写 CONFIG」的读改写,
 * 会和摄像头的 SCCB 抢同一条 I2C 总线 (SDA=3/SCL=2), 反而更容易掉帧。
 */
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
    ESP_LOGI(TAG, "按键已配置 (BOOT=GPIO%d=事件, K1(XIO%d)=音量-, K2(XIO%d)=音量+)",
             BTN_BOOT_GPIO, XIO_KEY_K1, XIO_KEY_K2);

    /* §3.9 看门狗: 订阅本任务 (老人唯一能主动求助的通道, 卡住必须复位) */
    reliability_watch_task(xTaskGetCurrentTaskHandle(), "button_task");

    /*
     * 每个键单独跟踪"上一次是否按下", 各自独立消抖。
     *
     * 为什么不用"三个键都松开才算一次操作结束"那种写法:
     *   那种写法下, 只要有一个键的电平卡在低 (硬件故障 / 外部拉低 /
     *   上拉没配好), 整个循环就永远出不来 —— 结果是另外两个正常的键
     *   也再也产生不了事件。
     *   实测就踩过这个坑: XIO1/K2 输入电平恒为 0, 结果 BOOT 物理按键
     *   的看护事件永远不生成, M2 验收直接卡死。
     *
     * 独立跟踪之后, 一个坏掉的键只会影响它自己, 不会再连累别人。
     *
     * 【armed 机制】
     *   AW9523B 没有内部上拉寄存器, K2 所在的 XIO1 在板上没有外部上拉,
     *   浮空时被读成低电平 (keytest 证明它并没有硬短到 GND)。
     *   如果直接拿"按下"当边沿, 开机那一刻就会误判成"按下了 K2"。
     *
     *   所以每个键都先"武装": 必须先观察到一次松开(高电平), 才认为
     *   后续的低电平是真实的按下。浮空的键永远等不到武装, 于是它
     *   既不会误触发, 也不会影响其他键。
     */
    bool prev_boot = false;
    bool prev_k1   = false;
    bool prev_k2   = false;
    bool armed_boot = false;   /* 见过松开才武装 */
    bool armed_k1   = false;
    bool armed_k2   = false;

    /*
     * 【长按撤销】状态机 (需求文档 §3.8)
     *
     * §3.8 要求:
     *   求助短按 -> 上报 help_pressed, 屏幕显示「已通知家人」,
     *               10s 内可长按撤销
     *   取消长按 -> 上报 help_canceled, 屏幕显示「求助已取消」
     *
     * 怎么区分短按和长按:
     *   【按下瞬间】就上报 help_pressed + 显示提示 —— 不能等松手才决定,
     *   否则老人按完就松手, 家人要等好几秒才收到, 体验很差。
     *   然后继续按住:
     *     - 3 秒内松开 -> 普通短按, 什么都不用做
     *     - 3 秒后还按住 -> 判定为长按, 上报 help_canceled
     *
     * 10s 窗口的含义:
     *   求助提示本身显示 10 秒 (PROMPT_DUR_HELP_SENT)。
     *   "可撤销"的窗口就是这 10 秒。超过 10 秒云端告警已经升级了,
     *   这时撤销就没意义了。
     */
    bool     help_pending = false;         /* 已上报求助, 等撤销 */
    int64_t  help_ts_ms = 0;               /* 上报求助的时刻 */
    bool     longpress_fired = false;      /* 本次按压已经触发过长按 */

    while (true) {
        bool boot_down = (gpio_get_level(BTN_BOOT_GPIO) == 0);
        bool k1_down    = aw_key_state(XIO_KEY_K1);
        bool k2_down    = aw_key_state(XIO_KEY_K2);

        /* ---- 武装: 观察到高电平(松开)才认为后续的低电平是真实按下 ---- */
        if (!boot_down) armed_boot = true;
        if (!k1_down)   armed_k1   = true;
        if (!k2_down)   armed_k2   = true;

        /* ---- BOOT 键: 产生看护事件 (按下瞬间触发, 松开重置) ---- */
        if (boot_down && !prev_boot && armed_boot) {
            vTaskDelay(pdMS_TO_TICKS(30));     /* 消抖: 30ms 后还在按住才算数 */

            if (gpio_get_level(BTN_BOOT_GPIO) == 0) {
                uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
                if (now - last_event_ms >= 300) {
                    last_event_ms = now;

                    button_event_t ev;
                    memset(&ev, 0, sizeof(ev));
                    ev.has_event = true;
                    ev.seq = now;
                    ev.ts_ms = esp_timer_get_time() / 1000;
                    snprintf(ev.message, sizeof(ev.message), "%s", BUTTON_MESSAGE);

                    ESP_LOGI(TAG, "检测到 BOOT 按键按下，生成事件: %s", ev.message);
                    xQueueSend(s_event_queue, &ev, 0);

                    /* §3.9: 推模式同步上报 (断网时缓存补传) */
                    push_event_to_cloud(ev.seq, ev.ts_ms, "button", ev.message);

                    /*
                     * §3.8 屏幕提示: 「已通知家人」
                     *
                     * 同时进入"可撤销"状态 —— 接下来 10 秒内长按可以撤销。
                     */
                    help_pending = true;
                    longpress_fired = false;
                    help_ts_ms = esp_timer_get_time() / 1000;
                    lcd_prompt_show(PROMPT_HELP_SENT, NULL, 0);
                }
            }
        }

        /*
         * 【长按撤销】检测。
         *
         * 按住超过 LONGPRESS_CANCEL_MS (3s) 且还没触发过长按 -> 触发一次。
         * 触发后就不再重复 —— 否则一直按着会刷出一堆 help_canceled。
         */
        if (boot_down && help_pending && !longpress_fired) {
            if ((esp_timer_get_time() / 1000) - help_ts_ms >= LONGPRESS_CANCEL_MS) {
                longpress_fired = true;
                help_pending = false;

                /* 上报 help_canceled —— 云端据此关闭该告警 */
                button_event_t ev;
                memset(&ev, 0, sizeof(ev));
                ev.has_event = true;
                ev.seq = (uint32_t)(esp_timer_get_time() / 1000);
                ev.ts_ms = esp_timer_get_time() / 1000;
                snprintf(ev.message, sizeof(ev.message), "%s", HELP_CANCEL_MESSAGE);

                ESP_LOGI(TAG, "检测到长按 (%.1f 秒)，撤销求助",
                         LONGPRESS_CANCEL_MS / 1000.0);
                xQueueSend(s_event_queue, &ev, 0);

                /* §3.9: 推模式同步上报 */
                push_event_to_cloud(ev.seq, ev.ts_ms, "button", ev.message);

                /* §3.8 屏幕提示: 「求助已取消」 */
                lcd_prompt_show(PROMPT_HELP_CANCELED, NULL, 0);
            }
        }

        /* ---- 松开: 结束本次按压 ---- */
        if (!boot_down && prev_boot) {
            /*
             * 短按松开 -> 撤销窗口关闭 (没撤销就当求助成立)。
             * 长按松开 -> 什么都不用做, 撤销已经在按住时上报了。
             */
            help_pending = false;
            longpress_fired = false;
        }

        prev_boot = boot_down;

        /* ---- K1/K2: 音量调节 (松开时触发, 避免长按连发) ---- */
        if (k1_down != prev_k1 || k2_down != prev_k2) {
            vTaskDelay(pdMS_TO_TICKS(30));

            bool k1_now = aw_key_state(XIO_KEY_K1);
            bool k2_now = aw_key_state(XIO_KEY_K2);

            if (k1_now != prev_k1 || k2_now != prev_k2) {
                /*
                 * 只有【已武装】的键才参与音量调节。
                 * 未武装 = 从开机到现在从没观察到松开 = 该脚悬空/故障,
                 * 不能拿它的低电平去改音量 (否则开机就自动加音量)。
                 */
                bool k1_valid = armed_k1 || k1_now;
                bool k2_valid = armed_k2 || k2_now;

                if (k1_valid && k2_valid) {
                    /* 两个键同时按 = 静音/取消静音 */
                    int v = audio_board_get_output_volume();
                    if (v <= 0) {
                        audio_board_set_output_volume(60);
                        ESP_LOGI(TAG, "取消静音, 音量 = 60");
                    } else {
                        audio_board_set_output_volume(0);
                        ESP_LOGI(TAG, "静音");
                    }
                } else if (k1_valid) {
                    int v = audio_board_get_output_volume();
                    if (v < 0) v = 60;
                    v -= 10;
                    if (v < 0) v = 0;
                    if (audio_board_set_output_volume(v) == 0) {
                        ESP_LOGI(TAG, "输出音量 = %d", v);
                    }
                } else if (k2_valid) {
                    int v = audio_board_get_output_volume();
                    if (v < 0) v = 60;
                    v += 10;
                    if (v > 100) v = 100;
                    if (audio_board_set_output_volume(v) == 0) {
                        ESP_LOGI(TAG, "输出音量 = %d", v);
                    }
                } else {
                    ESP_LOGW(TAG, "K1/K2 状态不可信 (未观察到松开), 忽略本次音量操作");
                }
            }

            prev_k1 = k1_now;
            prev_k2 = k2_now;
        }

        /*
         * §3.9 看门狗: 喂狗。
         *
         * 按键任务是【最关键】的任务之一 —— 老人出事时唯一能主动做的
         * 就是按这个键。这个任务卡住 = 求助功能失效 = 必须复位。
         */
        reliability_feed_watchdog();

        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

/* ---------------- 主函数 ---------------- */

/* 电源状态变化回调 (定义在文件后部, 供 app_main 注册) */
static void power_status_changed(const power_status_t *now,
                                 const power_status_t *old);

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
    mem_snapshot("nvs 初始化后");

    /*
     * 【2026-11-12】读 NVS 里存的 WiFi 配置 (换网不用重新烧固件)。
     *
     * 必须在 wifi_init() 之前调用 —— wifi_init 会用这里的值去
     * esp_wifi_set_config()。读不到就退回编译期默认值, 不是错误。
     */
    if (!wifi_config_load()) {
        ESP_LOGI(TAG, "使用编译期默认 WiFi 配置: SSID=\"%s\"", WIFI_SSID);
    }

    s_event_queue = xQueueCreate(8, sizeof(button_event_t));
    if (s_event_queue == NULL) {
        ESP_LOGE(TAG, "事件队列创建失败");
        return;
    }

    /*
     * 内存占用必须看【内部 RAM】, 不能看 esp_get_free_heap_size() ——
     * 后者统计的是总堆 (含 8MB PSRAM), 看起来永远很充裕, 但内部 RAM
     * 早就被吃光了 (任务 TCB 分配失败就是这么来的)。
     */
    ESP_LOGI(TAG, "PSRAM: %u bytes, 内部RAM剩余: %u bytes (最小 %u), 总堆剩余: %u",
             (unsigned int)esp_psram_get_size(),
             (unsigned int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned int)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
             (unsigned int)esp_get_free_heap_size());

    /* G.711 自检 —— 语音编码的量化逻辑写错了不会崩溃, 只会"听起来不对",
     * 所以开机先拿 ITU-T 官方参考向量验一遍。 */
    g711_selftest();

    /* I2C 引脚物理探针 —— 必须在任何 I2C 操作之前跑 */
    i2c_pin_physical_probe();

    board_i2c_init();

    /* I2C 多速率探测 —— 判断是不是上拉/时序问题 */
    i2c_speed_probe();

    /*
     * 先做一次全地址扫描, 搞清楚总线上到底有什么。
     *
     * 【为什么要放在 camera_power_reset() 之前】
     *   AW9523B 探测失败时, 我们需要知道:
     *     - 是地址错了?      -> 扫描里能看到 0x58 或 0x59 应答
     *     - 是总线被钳死?    -> SDA/SCL 电平全是 0 且无任何地址应答
     *     - 是器件没上电?    -> 其他器件 (codec/摄像头) 应答, 只有 AW 不应答
     *   这些结论决定了后面是"改地址"还是"修总线"还是"等上电",
     *   所以必须先看全局, 不要一头扎进重试里。
     */
    i2c_scan_once();
    task_start(i2c_monitor_task, "i2c_mon", 4096, 3);
    mem_snapshot("I2C 初始化后");

    camera_power_reset();

#if AUDIO_I2C_SCAN_ENABLE
    /* 音频 codec 探测 (只在需要排查硬件时打开) */
    i2c_scan_audio();
#endif

    /* 屏幕: 背光已在 AW9523B 里打开, 这里初始化面板驱动 */
    bool lcd_ok = (lcd_init() == ESP_OK);
    mem_snapshot("lcd_init 后");
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
    /*
     * 【2026-11-05 删除】原来的诊断副本缓冲 (38400 字节内部 RAM) 已移除:
     * 它从来没被用过, 却把内部 RAM 挤到只剩 16.9KB,
     * 直接导致 LCD 双缓冲申请失败 -> 屏幕全黑。
     */

    /* 原始帧快照 (取证用, 走 /raw 端点导出) */
    s_raw_snapshot = heap_caps_malloc(RAW_SNAPSHOT_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_raw_snapshot == NULL) {
        ESP_LOGE(TAG, "原始帧快照缓冲分配失败");
    } else {
        ESP_LOGI(TAG, "原始帧快照缓冲就绪 (%" PRIu32 " 字节)", (uint32_t)RAW_SNAPSHOT_BYTES);
    }

    ESP_LOGI(TAG, "帧槽位就绪: %d 个 RGB565(%u字节) + %d 个 JPEG(%u字节)",
             SLOT_COUNT, (unsigned)RGB565_SLOT_BYTES,
             SLOT_COUNT, (unsigned)JPEG_SLOT_BYTES);

    camera_init();
    mem_snapshot("camera_init 后");
#endif

    /*
     * 活动检测 (需求文档 §3.5)。
     *
     * 必须在 camera_init() 之后 —— 分析器要拿到真实帧尺寸。
     * 但要在 Wi-Fi / HTTP 之前, 因为它要分配 PSRAM 缓冲。
     */
    if (activity_detect_init() == ESP_OK) {
        ESP_LOGI(TAG, "活动检测已就绪 (GET /activity 查看状态)");
    }
    mem_snapshot("活动检测后");

    /*
     * 屏幕提示层 (需求文档 §3.8)。
     *
     * 在摄像头之后启动 —— 提示任务要往屏幕上画横幅,
     * 必须等 LCD 初始化完 (lcd_st7789_init) 否则画上去也是乱的。
     */
    if (lcd_prompt_start() == ESP_OK) {
        ESP_LOGI(TAG, "屏幕提示已就绪 (短按求助/长按撤销/家人指令提示)");
    }

    /*
     * 音频: ES8311 (播放 DAC) + ES7210 (麦克风 ADC)。
     * 放在摄像头之后 —— 摄像头初始化要抢 SPI/GDMA, 先让它拿到资源。
     */
    if (audio_board_init() == ESP_OK) {
        ESP_LOGI(TAG, "音频子系统就绪");
        mem_snapshot("audio_board_init 后");
        /*
         * 开机自检: 播放 100ms 测试音 + 录 800ms 统计电平。
         * 这是启动健康检查 —— 如果 codec 没起来, 这里会打警告。
         * 波形缓冲放堆上 —— app_main 的栈只有 8192 字节, 放不下 1600 帧。
         */
        int16_t *wave = heap_caps_malloc(512 * sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        int peak = audio_board_selftest(800, wave, 512);
        ESP_LOGI(TAG, "音频自检完成, 录音峰值=%d", peak);
        if (wave) free(wave);

        /*
         * 起录音监测任务: 每秒打一条电平。对着麦克风说话时 rms 应该明显
         * 抬高 (日志里会显示 [有声音])。这是验证麦克风是否真的接通的
         * 最直接办法 —— 比"读到了非零数据"更有说服力。
         */
        if (audio_board_start_monitor() == ESP_OK) {
            ESP_LOGI(TAG, "录音监测已启动, 对着麦克风说话看 rms 变化");
        }
        mem_snapshot("音频监测启动后");

#if AUDIO_SELFTONE
        /*
         * 开机自动循环播测试音。
         *
         * 目的: 验证喇叭链路 (ES8311 DAC -> 功放 -> 喇叭) 真的通。
         * I2S 写入成功只能证明软件侧没问题, 功放没使能/喇叭虚焊这类
         * 硬件问题只能靠耳朵听出来。调试完记得改回 0。
         */
        if (audio_board_start_tone() == ESP_OK) {
            ESP_LOGI(TAG, "测试音已自动启动 (每 3 秒响 1 秒), 请听喇叭");
        }
#endif
    } else {
        ESP_LOGW(TAG, "音频初始化失败，视频功能不受影响");
    }

    /*
     * 电池 / 电源检测 (BOX3 底板 18650 电池座 + 充电电路)。
     *
     * 放在摄像头之后、Wi-Fi 之前:
     *   - 需要 AW9523B 已经初始化好 (采样时序要操作 XIO2)
     *   - ADC1_CH0 (GPIO1) 独占 ADC1, 不与摄像头/音频抢资源
     *
     * 作用: 让软件能区分"被人拔了电"和"正常充电中",
     *       这是产品化断电告警 (PRD R-05) 的基础。
     */
    if (power_board_init() == ESP_OK) {
        ESP_LOGI(TAG, "电源管理已就绪 (串口命令 bat 查看电量)");
        /*
         * 注册变化回调 —— 插拔 Type-C / 开始充电停止充电时,
         * 自动推一条事件给子女端, 让他知道设备是"断电"还是"断网"。
         */
        power_board_set_change_cb(power_status_changed);
        power_board_start_monitor();
    } else {
        ESP_LOGW(TAG, "电源管理初始化失败, 无法读取电池电量");
    }

    /*
     * ============ AI 对话客户端 ============
     *
     * 启动后台任务, 让板子能直接跟服务器上的大模型对话。
     *
     * 为什么放在最后启动:
     *   音频子系统 (audio_board_init) 必须已经就绪, 否则采集不到语音。
     *   失败也不影响其它功能 —— 视频、按键、告警照常。
     */
    if (ai_client_start() == ESP_OK) {
        ESP_LOGI(TAG, "AI 对话客户端已启动 (串口命令 ai 手动发起一轮对话)");
    } else {
        ESP_LOGW(TAG, "AI 对话客户端启动失败, 语音对话不可用");
    }

    /*
     * ============ 离线唤醒词「晴老师」 ============
     *
     * 必须放在 ai_client_start() 之后: 唤醒词命中时会调
     * ai_client_start_turn_at(), 那边得已经起来了。
     *
     * 失败不影响其它功能 —— 老人还可以用按键/手环触发告警,
     * 串口 "ai go" 也能手动发起对话。
     */
    if (wake_word_start() == ESP_OK) {
        ESP_LOGI(TAG, "唤醒词「%s」已就绪 (串口命令 wake 查看状态)", "晴老师");
        mem_snapshot("wake_word_start 后");
        /*
         * 一套固件两种用法: 默认看护对讲, 喊「小康同学」切到小智。
         * 打一行状态, 开机日志里就能确认两句口令都注册上了。
         */
        voice_mode_dump_status();
    } else {
        ESP_LOGW(TAG, "唤醒词启动失败, 只能手动触发对话 (串口 ai go)");
    }

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
            lcd_draw_rgb565(fb->buf, fb->width * 2, 0, 0, fb->width, fb->height, true, true);

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
     *
     * 注意: 这个宏定义必须在文件靠前的位置 (这里), 因为上面的
     * audio_selftest_loop() 里也用 #if WIFI_ENABLE 做编译期分支。
     * 之前它定义在文件末尾, 那里前面的代码看到的 WIFI_ENABLE 是 0,
     * 导致自环测试直接被编译掉了。
     */
    wifi_init();
    mem_snapshot("wifi_init 后");
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
    mem_snapshot("Wi-Fi 等待结束");
#endif /* WIFI_ENABLE */

#if WIFI_ENABLE
    http_server_start();
    mem_snapshot("http_server_start 后");

    /*
     * 可靠性模块 (需求文档 §3.9)。
     *
     * 必须在 Wi-Fi 连上之后启动 —— 心跳要读 RSSI 和 IP。
     */
    if (reliability_start() == ESP_OK) {
        ESP_LOGI(TAG, "可靠性模块已启动 (心跳 %d ms / 看门狗 / 降级模式)",
                 RELIABILITY_HEARTBEAT_MS);
    }

    /*
     * 推模式客户端 (需求文档 §3.9 断网缓存补传)。
     *
     * 连不上电脑端也没关系 —— 后台会一直按指数退避重试,
     * 期间产生的事件会缓存起来 (N=20), 连上后自动补传。
     */
    if (push_client_start() == ESP_OK) {
        ESP_LOGI(TAG, "推模式客户端已启动 (缓存 %d 事件 + %d 帧 JPEG)",
                 PUSH_EVENT_MAX, PUSH_JPEG_MAX);
    }
#endif

    /*
     * USB-Serial-JTAG 驱动。
     *
     * 串口调试命令要直接从 USB 设备读字节, 但这个驱动默认没装
     * (IDF 只把它当"次要 console"用来转发输出, 不装驱动就没法读)。
     * 装上之后 usb_serial_jtag_read_bytes() 就能拿到 COM3 发来的字节。
     *
     * 注意: 装了驱动以后, ESP_LOGI 的输出依然走 IDF 的双路 console,
     * COM3 上照常能看到日志, 两者不冲突 (一个只读 USB RX, 一个只写)。
     */
    if (!usb_serial_jtag_is_driver_installed()) {
        usb_serial_jtag_driver_config_t usb_cfg = {
            .rx_buffer_size = 1024,
            .tx_buffer_size = 1024,
        };
        esp_err_t uerr = usb_serial_jtag_driver_install(&usb_cfg);
        if (uerr != ESP_OK) {
            ESP_LOGW(TAG, "USB-Serial-JTAG 驱动安装失败 (%s), 串口命令不可用",
                     esp_err_to_name(uerr));
        } else {
            ESP_LOGI(TAG, "USB-Serial-JTAG 驱动已就绪, 串口命令可用");
        }
    }

    task_start(button_task, "button_task", 4096, 5);
    task_start(aw9523b_diag_task, "aw_diag", 4096, 4);
#if WIFI_ENABLE && !WIFI_MODE_AP
    task_start(wifi_reconnect_task, "wifi_recon", 4096, 4);
#endif
    mem_snapshot("建完基础任务后");

#if LCD_SELFTEST_ONLY == 0
    /*
     * 生产者 (唯一碰摄像头的任务) 优先级最高 —— 它一停, 整条链路都停。
     * LCD 预览优先级最低, 它只是"老人看得见"的辅助, 让位给视频流。
     */
    task_start(camera_producer_task, "cam_producer", 6144, 6);
    if (lcd_ok) {
        task_start(lcd_preview_task, "lcd_preview", 6144, 2);
    /*
     * uart_cmd 栈给 8192 —— 里面要跑 esp_http_client (自环测试),
     * 那个库的 TLS/DNS/解析路径吃栈不少, 4096 会 stack overflow。
     */
    task_start(uart_cmd_task, "uart_cmd", 8192, 1);
    }
#endif

    ESP_LOGI(TAG, "内部剩余=%u 最小剩余=%u",
             (unsigned int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned int)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL));

    printf("\n固件启动完成\n\n");
}
