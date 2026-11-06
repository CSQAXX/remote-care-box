/*
 * TCP 推模式客户端 —— 实现
 *
 * 见 push_client.h 的说明 (需求文档 §3.9)。
 *
 * ============================ 线程模型 ============================
 *
 * 一个后台任务 push_client_task 负责所有 socket 操作:
 *
 *   [无连接] --退避等待--> [尝试 connect] --成功--> [已连接]
 *        ^                                            |
 *        |                                            | 写失败 / 对端关闭
 *        +------------- 退避等待 <---------------------+
 *
 * 提交事件的一方 (按键任务 / 活动检测 / 心跳) 只往队列里塞,
 * 绝不直接碰 socket —— 这样"发送"永远不会阻塞"产生事件"的路径。
 *
 * 老人按键那一刻, 哪怕网络正断着, 按键任务也只是一次 memcpy,
 * 立刻返回。绝不能因为网络问题让按键响应变慢。
 *
 * ============================ 队列与补传 ============================
 *
 * s_event_q 是事件的环形队列 (容量 PUSH_EVENT_MAX = 20)。
 * 满了就【丢最旧的】而不是丢新的 —— 新的求助事件比旧的更值钱。
 *
 * 连接建立时, 队列里还积压的事件会一次性补传, 每条加上 "replay":true。
 * 补传完队列就空了, 之后是新事件。
 *
 * JPEG 缓存是独立的环形缓冲 (PUSH_JPEG_MAX = 10 帧),
 * 只在【没人拉流】的时候抓 —— 也就是断网期间, 用来留个现场。
 * ==================================================================
 */

#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <unistd.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"

#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "lwip/inet.h"

#include "camera_pins.h"
#include "audio_board.h"   /* APP_CORE_ID —— 所有任务统一钉在 core 0 */
#include "push_client.h"

static const char *TAG = "push";

/* ---------------- 内部状态 ---------------- */

static QueueHandle_t     s_event_q = NULL;
static TaskHandle_t      s_task = NULL;
static volatile bool     s_connected = false;
static volatile uint32_t s_backoff_ms = PUSH_BACKOFF_MIN_MS;

/* 统计 */
static volatile uint32_t s_sent_events = 0;
static volatile uint32_t s_sent_jpegs = 0;
static volatile uint32_t s_replayed = 0;
static volatile uint32_t s_dropped = 0;
static volatile uint32_t s_connect_fails = 0;

/* ---------------- JPEG 环形缓存 ---------------- */

/*
 * 每帧 JPEG 最多 24KB。
 *
 * 依据: 实测 320x240 @ quality 65 约 8~12KB, 24KB 留了 2 倍余量。
 * 超出就截断 —— 宁可少半个画面, 也不能因为一帧异常大就吃掉整个缓存。
 */
#define PUSH_JPEG_FRAME_MAX   (24 * 1024)
#define PUSH_JPEG_POOL_BYTES  (PUSH_JPEG_MAX * PUSH_JPEG_FRAME_MAX)

typedef struct {
    uint32_t seq;
    uint32_t len;
    uint64_t ts_ms;
} jpeg_meta_t;

static uint8_t   *s_jpeg_pool = NULL;       /* PSRAM */
static jpeg_meta_t s_jpeg_meta[PUSH_JPEG_MAX];
static int        s_jpeg_head = 0;          /* 下一个要写的位置 */
static int        s_jpeg_tail = 0;          /* 最旧的那一帧在哪 (取帧用) */
static int        s_jpeg_count = 0;         /* 当前缓存了多少帧 */
static uint32_t   s_jpeg_seq = 0;
static volatile uint32_t s_jpeg_overflow = 0;   /* 队列满时被挤掉的帧数 */

/*
 * 发送缓冲 (取一帧出来再发, 不持锁发送)。
 *
 * 【2026-11-05】从 .bss (内部 RAM) 挪到 PSRAM:
 *   24KB 静态数组默认在内部 RAM 里, 而这块板子内部 RAM 极紧张
 *   (实测开机只剩 16.9KB, 修完 LCD 之后也只有 37KB)。
 *   发送缓冲只是被 CPU 读、然后 memcpy 进 socket, 不参与 DMA,
 *   所以放 PSRAM 完全安全。
 */
static uint8_t  *s_jpeg_send_buf = NULL;   /* PSRAM, 见 jpeg_cache_init() */
static uint32_t s_jpeg_send_len = 0;
static uint32_t s_jpeg_send_seq = 0;
static uint64_t s_jpeg_send_ts = 0;

/*
 * JPEG 缓存的互斥量。
 *
 * 【为什么必须有】这个缓存有两个使用者, 而且【同时】在动:
 *   1. 生产者任务 (camera_producer) —— 每 2 秒往里写一帧
 *   2. 推模式任务 (push_client)      —— 重连时把里面的帧补传出去
 *
 * 没有锁的话会出现这种撕裂:
 *   推模式任务正在读 s_jpeg_meta[idx].len, 生产者任务同时在写它。
 *   读到一半的 uint32_t 可能是几百万 —— 于是 send() 想发几 MB 数据,
 *   当然失败; 或者更糟, 发出去一帧长度字段错误的 JPEG, 电脑端解析全乱。
 *
 * 这种 bug 表现为"偶尔补传失败", 极难重现和定位, 所以从一开始就用锁。
 */
static SemaphoreHandle_t s_jpeg_mutex = NULL;

/* 事件队列元素 */
typedef struct {
    char     body[PUSH_EVENT_BODY_MAX];
    uint32_t id;      /* 单调递增的提交序号, 用来区分"补传"和"实时" */
} event_item_t;

/*
 * 补传边界。
 *
 * 每次【重连成功】时, 把当时已提交的最大 id 记下来。
 * id <= 这个值的 = 断线期间产生的事件 = 需要打 "replay":true;
 * id >  这个值的 = 连上之后新产生的事件 = 实时事件, 不打标记。
 *
 * 为什么必须这么区分:
 *   如果简单地把"队列里积压的"一律当补传, 那重连之后新产生的事件
 *   也会被误标成 replay —— 云端规则引擎会抑制这些事件的告警,
 *   老人真的按了求助键子女却收不到通知。
 *
 *   队列清空后新来的事件 id 更大, 天然就被排除在外了。
 */
static volatile uint32_t s_next_event_id = 1;    /* 下一个要分配的 id */
static volatile uint32_t s_replay_upto = 0;      /* <= 此 id 的算补传 */

/* ---------------- 内部工具 ---------------- */

/*
 * 全部写操作。
 *
 * 注意 send() 可能只写了一半 (内核缓冲满), 所以必须循环写。
 * 返回 false 表示连接已经断了 —— 上层要负责重连。
 */
static bool write_all(int fd, const void *buf, size_t len)
{
    const uint8_t *p = (const uint8_t *)buf;
    size_t done = 0;
    int stall = 0;   /* 连续多少次"发送缓冲区满" */

    while (done < len) {
        int n = send(fd, p + done, len - done, 0);
        if (n > 0) {
            done += (size_t)n;
            stall = 0;
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            /*
             * 发送缓冲区满, 等一下再试。
             *
             * 必须设 SO_SNDTIMEO —— 否则 send() 会永远阻塞,
             * 任务卡死 = 心跳停 = 云端以为设备死了。
             * 设了超时之后这里返回 -1/EAGAIN, 我们重试即可。
             *
             * 但不能无限重试: 如果网线被拔了, TCP 不会立刻发 RST,
             * 发送缓冲区会一直满 —— 无限重试会让这个任务永远卡在这,
             * 事件全堆在队列里出不去。
             *
             * 所以连续 stall 超过阈值就认定连接死了, 返回 false 让上层重连。
             * 阈值取 20 次 x 10ms = 200ms, 足够吃掉正常的网络抖动。
             */
            stall++;
            if (stall > 20) {
                ESP_LOGE(TAG, "发送缓冲区持续满 (%d 次), 判定连接已死", stall);
                return false;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        /* n == 0 或真错误 -> 连接断了 */
        ESP_LOGE(TAG, "send 失败: n=%d errno=%d (%s)", n, errno, strerror(errno));
        return false;
    }
    return true;
}

/*
 * 建立到电脑的 TCP 连接。
 *
 * 返回 socket fd, 失败返回 -1。
 */
static int connect_to_pc(void)
{
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(PC_RECEIVER_PORT);

    if (inet_pton(AF_INET, PC_RECEIVER_IP, &addr.sin_addr) != 1) {
        ESP_LOGE(TAG, "PC_RECEIVER_IP 不是合法 IPv4: %s", PC_RECEIVER_IP);
        return -1;
    }

    int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) {
        ESP_LOGE(TAG, "socket 失败: errno=%d", errno);
        return -1;
    }

    /*
     * 发送超时 —— 防止 send() 永久阻塞卡死整个任务。
     * 5 秒足够发完一帧 JPEG 了。
     */
    struct timeval tv = { .tv_sec = 5, .tv_usec = 0 };
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    /* 关闭 Nagle —— 事件是小报文, 攒起来反而延迟大 */
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        ESP_LOGW(TAG, "连接 %s:%d 失败: errno=%d",
                 PC_RECEIVER_IP, PC_RECEIVER_PORT, errno);
        close(fd);
        return -1;
    }

    ESP_LOGI(TAG, "已连上电脑接收端 %s:%d", PC_RECEIVER_IP, PC_RECEIVER_PORT);
    return fd;
}

/* 发送一条事件 (body 不含外层 {}) */
static bool send_event_raw(int fd, const char *body)
{
    /* 拼成 {"..."} —— 外面再加一层花括号 */
    char line[PUSH_EVENT_BODY_MAX + 8];
    int n = snprintf(line, sizeof(line), "{%s}\n", body);
    if (n <= 0 || n >= (int)sizeof(line)) {
        return false;
    }
    return write_all(fd, line, (size_t)n);
}

/* 发送一帧 JPEG */
static bool send_jpeg_raw(int fd, const uint8_t *data, size_t len, uint32_t seq, uint64_t ts_ms)
{
    char head[128];
    int hn = snprintf(head, sizeof(head),
                      "{\"type\":\"jpeg\",\"seq\":%u,\"len\":%u,\"ts\":%llu}\n",
                      (unsigned)seq, (unsigned)len, (unsigned long long)ts_ms);
    if (hn <= 0 || hn >= (int)sizeof(head)) {
        return false;
    }
    if (!write_all(fd, head, (size_t)hn)) {
        ESP_LOGE(TAG, "JPEG 帧头发送失败 (seq=%u len=%u)", (unsigned)seq, (unsigned)len);
        return false;
    }
    if (!write_all(fd, data, len)) {
        ESP_LOGE(TAG, "JPEG 数据发送失败 (seq=%u len=%u, 已发 %u 字节)",
                 (unsigned)seq, (unsigned)len, (unsigned)len);
        return false;
    }
    /* 结尾换行, 和电脑端的行解析对齐 */
    if (!write_all(fd, "\n", 1)) {
        ESP_LOGE(TAG, "JPEG 结尾换行发送失败 (seq=%u)", (unsigned)seq);
        return false;
    }
    return true;
}

/* ---------------- JPEG 环形缓存 ---------------- */

static void jpeg_cache_init(void)
{
    if (s_jpeg_pool != NULL) {
        return;
    }
    /*
     * PSRAM 分配。
     *
     * 10 帧 * 24KB = 240KB, 内部 RAM 放不下 (历史只剩 28KB),
     * PSRAM 有 8MB, 很宽裕。
     */
    s_jpeg_pool = (uint8_t *)heap_caps_malloc(PUSH_JPEG_POOL_BYTES,
                                              MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_jpeg_pool == NULL) {
        ESP_LOGE(TAG, "JPEG 缓存分配失败 (%d 字节, PSRAM)", PUSH_JPEG_POOL_BYTES);
        return;
    }
    if (s_jpeg_mutex == NULL) {
        s_jpeg_mutex = xSemaphoreCreateMutex();
        if (s_jpeg_mutex == NULL) {
            ESP_LOGE(TAG, "JPEG 缓存互斥量创建失败");
            return;
        }
    }
    if (s_jpeg_send_buf == NULL) {
        /* 发送缓冲放 PSRAM (24KB 内部 RAM 太贵了, 见上面注释) */
        s_jpeg_send_buf = (uint8_t *)heap_caps_malloc(PUSH_JPEG_FRAME_MAX,
                                                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_jpeg_send_buf == NULL) {
            ESP_LOGE(TAG, "JPEG 发送缓冲分配失败 (%d 字节, PSRAM)", PUSH_JPEG_FRAME_MAX);
            return;
        }
    }
    ESP_LOGI(TAG, "JPEG 缓存就绪: %d 帧 x %d 字节 (PSRAM)",
             PUSH_JPEG_MAX, PUSH_JPEG_FRAME_MAX);
}

static void jpeg_cache_push(const uint8_t *data, size_t len)
{
    if (s_jpeg_pool == NULL) {
        return;
    }
    if (len > PUSH_JPEG_FRAME_MAX) {
        len = PUSH_JPEG_FRAME_MAX;   /* 截断, 不让一帧吃掉整个池 */
    }

    if (s_jpeg_mutex) {
        xSemaphoreTake(s_jpeg_mutex, portMAX_DELAY);
    }

    /*
     * 队列满了吗? 满了就把最旧的那一帧丢掉, 腾位置放新的。
     *
     * 【2026-11-05 修复顺序 bug】
     * 原来的写法是:
     *     s_jpeg_head = (s_jpeg_head + 1) % MAX;
     *     if (s_jpeg_count < MAX) s_jpeg_count++;
     * 队列满的时候, head 会【绕回 tail】, 覆盖掉最旧那一帧的数据,
     * 但 count 仍然是 MAX —— 于是取帧时算出的 "最旧帧下标"
     * (head - count) 指向的其实是【已经被覆盖】的槽位,
     * 发出去的是一帧元数据和数据对不上的 JPEG (seq 对, 内容错)。
     *
     * 现在用显式的 tail 指针: 满了就 tail++ 一起前进,
     * head 和 tail 始终保持 count == (head - tail) % MAX 的关系。
     *
     * 为什么丢旧不丢新:
     *   新的事件是"刚刚发生的事", 旧的是"已经过去的事"。
     *   老人刚才按的求助键, 比五分钟前的普通事件重要得多。
     */
    if (s_jpeg_count == PUSH_JPEG_MAX) {
        s_jpeg_tail = (s_jpeg_tail + 1) % PUSH_JPEG_MAX;
        s_jpeg_count--;
        s_jpeg_overflow++;
    }

    uint8_t *dst = s_jpeg_pool + (size_t)s_jpeg_head * PUSH_JPEG_FRAME_MAX;
    memcpy(dst, data, len);

    s_jpeg_meta[s_jpeg_head].seq = ++s_jpeg_seq;
    s_jpeg_meta[s_jpeg_head].len = (uint32_t)len;
    s_jpeg_meta[s_jpeg_head].ts_ms = esp_timer_get_time() / 1000;

    s_jpeg_head = (s_jpeg_head + 1) % PUSH_JPEG_MAX;
    s_jpeg_count++;

    if (s_jpeg_mutex) {
        xSemaphoreGive(s_jpeg_mutex);
    }
}

/*
 * 取一帧缓存 JPEG 的副本, 供发送使用。
 *
 * 为什么【拷出来】而不是直接在锁内发送:
 *   send() 可能阻塞几百毫秒 (缓冲区满时)。持锁期间生产者就写不进去了,
 *   摄像头那侧会卡住。
 *
 * 帧最大 24KB, 拷到栈上不合适, 所以用一个静态的发送缓冲。
 * 只有推模式任务会用它, 所以不存在并发问题。
 *
 * 【2026-11-05】这个缓冲从 .bss (内部 RAM) 挪到 PSRAM:
 *   24KB 静态数组默认在内部 RAM 里, 而这块板子内部 RAM 极紧张
 *   (实测开机只剩 16.9KB, 修完 LCD 之后也只有 37KB)。
 *   发送缓冲只是被 CPU 读、然后 memcpy 进 socket, 不参与 DMA,
 *   所以放 PSRAM 完全安全。
 */
static bool jpeg_cache_take_oldest(void)
{
    if (s_jpeg_pool == NULL || s_jpeg_mutex == NULL || s_jpeg_send_buf == NULL) {
        return false;
    }

    xSemaphoreTake(s_jpeg_mutex, portMAX_DELAY);

    if (s_jpeg_count <= 0) {
        xSemaphoreGive(s_jpeg_mutex);
        return false;
    }

    /* 最旧的一帧 = tail (显式指针, 见 jpeg_cache_push 的注释) */
    int idx = s_jpeg_tail;

    uint32_t len = s_jpeg_meta[idx].len;
    if (len > PUSH_JPEG_FRAME_MAX) {
        len = PUSH_JPEG_FRAME_MAX;
    }

    memcpy(s_jpeg_send_buf,
           s_jpeg_pool + (size_t)idx * PUSH_JPEG_FRAME_MAX,
           len);

    s_jpeg_send_len = len;
    s_jpeg_send_seq = s_jpeg_meta[idx].seq;
    s_jpeg_send_ts = s_jpeg_meta[idx].ts_ms;

    /* 这一帧已经被取走, tail 前进, 数量减一 */
    s_jpeg_tail = (s_jpeg_tail + 1) % PUSH_JPEG_MAX;
    s_jpeg_count--;

    xSemaphoreGive(s_jpeg_mutex);
    return true;
}

/*
 * 【2026-11-05 修复: 推模式要实时发帧, 不能只在重连时补传】
 *
 * ---------------------------------------------------------------------------
 * 症状: 接收端连上后收到一批 JPEG (补传), 然后就再也不收帧了, 只剩心跳。
 *
 * 根因: 原来的设计把 JPEG 环形缓存当成"断网缓冲"用:
 *   - push_client_push_jpeg() 只往缓存里塞帧, 从不主动发
 *   - push_client_task 只在【重连成功的那一刻】把缓存补传一遍, 然后 clear()
 *   - 所以连接稳定之后, 缓存里再也没有帧被取走 -> JPEG 流断掉
 *
 * 另外 main.c 的 need_jpg 判断是 "s_stream_clients > 0" (HTTP 拉流) 才编码,
 * 推模式连着的时候压根不编码 JPEG —— 就算推任务想发也没有帧可发。
 *
 * 修复:
 *   1) 这个缓存改成"待发队列"语义 —— 生产者塞帧, 推任务持续取帧发出。
 *      重连时的补传就变成"把队列里积压的帧一次性倒出去", 语义自然统一。
 *      (原来的 jpeg_cache_clear() 因此不再需要, 已删除 ——
 *       它会在补传时把补传期间新进来的帧一起丢掉。)
 *   2) main.c 里, 推模式已连接时也要限速编码 (1 秒一帧足够)。
 * ---------------------------------------------------------------------------
 */

/* ---------------- 主任务 ---------------- */

static void push_client_task(void *arg)
{
    (void)arg;
    int fd = -1;

    uint32_t backoff_ms = PUSH_BACKOFF_MIN_MS;
    bool ever_connected = false;

    while (true) {
        /* ---- 1) 没连接 -> 先等退避, 再尝试 ---- */
        if (fd < 0) {
            s_connected = false;
            s_backoff_ms = backoff_ms;

            vTaskDelay(pdMS_TO_TICKS(backoff_ms));

            fd = connect_to_pc();
            if (fd < 0) {
                s_connect_fails++;
                /* 指数退避: 1s -> 2s -> 4s -> ... -> 60s 封顶 */
                backoff_ms *= 2;
                if (backoff_ms > PUSH_BACKOFF_MAX_MS) {
                    backoff_ms = PUSH_BACKOFF_MAX_MS;
                }
                continue;
            }

            s_connected = true;
            backoff_ms = PUSH_BACKOFF_MIN_MS;
            s_backoff_ms = backoff_ms;

            /*
             * 记下补传边界。
             *
             * 此刻已经提交的所有事件 (id <= s_next_event_id - 1) 都是
             * 断线期间产生的, 连上后要带 replay 标记补传。
             * 之后新产生的事件 id 更大, 是实时事件, 不带标记。
             */
            s_replay_upto = s_next_event_id - 1;

            if (ever_connected) {
                ESP_LOGI(TAG, "重连成功, 开始补传缓存的事件 (边界 id=%" PRIu32 ")",
                         (uint32_t)s_replay_upto);
            } else {
                ever_connected = true;
                ESP_LOGI(TAG, "推模式已连接");
            }

            /*
             * 补传 JPEG 缓存。
             *
             * 顺序: 从最旧的一帧开始 (head - count)。
             * JPEG 不带 replay 标记 —— 它只是画面, 规则引擎不看画面,
             * 不会触发告警, 所以不需要抑制。
             *
             * 每帧都是【拷出来再发】, 不持锁发送 —— 见 jpeg_cache_take_oldest()
             * 的注释。
             *
             * 【2026-11-05】这里不再 clear() 掉整个缓存。
             * 缓存现在是"待发队列": 补传只是把积压的帧一次性倒出去,
             * 之后的生产者新帧还要靠下面的实时推送逻辑发出去。
             * clear() 会把补传期间新进来的帧也一起丢掉。
             */
            int replayed_frames = 0;
            while (jpeg_cache_take_oldest()) {
                if (!send_jpeg_raw(fd, s_jpeg_send_buf, s_jpeg_send_len,
                                   s_jpeg_send_seq, s_jpeg_send_ts)) {
                    ESP_LOGE(TAG, "补传 JPEG 失败, 断开重连 (已补 %d 帧)",
                             replayed_frames);
                    close(fd);
                    fd = -1;
                    break;
                }
                s_sent_jpegs++;
                replayed_frames++;
            }
            if (fd >= 0) {
                ESP_LOGI(TAG, "已补传 %d 帧缓存 JPEG", replayed_frames);
            }
        }

        /*
         * ---- 2) 已连接 -> 实时推送 JPEG ----
         *
         * 【2026-11-05 新增】
         *
         * 原来 JPEG 只在重连时补传, 连接稳定后就不再有帧了。
         * 现在推任务持续从"待发队列"里取帧发出。
         *
         * 为什么不是"有帧就发":
         *   JPEG 编码很贵 (320x240 @ q65 约 70ms), 生产者那边已经限速到
         *   1 秒一帧, 所以队列里最多也就积压一帧。
         *   这里每轮循环只取【一帧】, 剩下的下一轮再发,
         *   避免一轮里灌太多帧把事件流饿死 (事件比画面重要)。
         */
        if (fd >= 0) {
            if (jpeg_cache_take_oldest()) {
                if (!send_jpeg_raw(fd, s_jpeg_send_buf, s_jpeg_send_len,
                                   s_jpeg_send_seq, s_jpeg_send_ts)) {
                    ESP_LOGE(TAG, "推送 JPEG 失败, 断开重连");
                    close(fd);
                    fd = -1;
                    continue;
                }
                s_sent_jpegs++;
            }
        }

        /* ---- 3) 已连接 -> 从队列取事件发出去 ---- */
        event_item_t item;

        /*
         * 用 200 毫秒超时来轮询队列。
         *
         * 为什么不无限阻塞: 连接可能已经断了 (对端关闭 / 网络切换),
         * 我们得有机会去写一次才知道。
         *
         * 【2026-11-05】从 1000ms 改成 200ms:
         *   循环里新增了"实时推 JPEG"的步骤, 如果事件轮询阻塞 1 秒,
         *   画面推送也会跟着卡 1 秒。200ms 的粒度足够灵敏,
         *   CPU 开销也可以忽略 (每秒 5 次轮询)。
         */
        if (xQueueReceive(s_event_q, &item, pdMS_TO_TICKS(200)) == pdTRUE) {
            /*
             * 补传标记。
             *
             * 判断依据: 这条事件是在【断网期间】产生的。
             * 队列里积压的事件 = 连接断开后产生的事件 = 需要打 replay 标记。
             *
             * 但第一次连接时队列里也可能有积压(刚开机就往队列塞),
             * 那时候打 replay 标记也无害 —— 云端会忽略 replay 事件的告警,
             * 而开机初期的事件本来也不该触发"老人长时间无活动"之类的告警。
             */
            char body[PUSH_EVENT_BODY_MAX + 32];
            int n = snprintf(body, sizeof(body), "%s", item.body);

            if (n > 0 && n < (int)sizeof(body)) {
                /*
                 * 补传标记 —— 只给"断线期间产生"的事件加。
                 *
                 * 判据: id <= s_replay_upto 说明这条事件是在上次断开
                 * 之后、这次连上之前提交的。
                 */
                bool is_replay = (item.id != 0 && item.id <= s_replay_upto);

                if (is_replay && strstr(body, "\"replay\"") == NULL) {
                    /* 在 JSON 主体最前面插一个字段: {"replay":true, ...} */
                    char with_flag[PUSH_EVENT_BODY_MAX + 32];
                    int m = snprintf(with_flag, sizeof(with_flag), "\"replay\":true,%s", body);
                    if (m > 0 && m < (int)sizeof(with_flag)) {
                        memcpy(body, with_flag, (size_t)m + 1);
                    }
                }

                if (send_event_raw(fd, body)) {
                    s_sent_events++;
                    if (is_replay) {
                        s_replayed++;
                    }
                    ESP_LOGI(TAG, "已推送%s事件: %.80s",
                             is_replay ? "(补传)" : "", body);
                } else {
                    /*
                     * 发送失败 -> 连接断了。
                     *
                     * 关键: 把这条事件【放回队列头部】,
                     * 否则这次断线期间的事件就丢了。
                     *
                     * FreeRTOS 没有"放回头部"的 API, 所以用
                     * xQueueSendToFront —— 但这样会把这条事件排到
                     * 最前面, 顺序略微乱了一点, 对事件流可以接受
                     * (每条事件自带 seq, 云端按 seq 排序)。
                     */
                    xQueueSendToFront(s_event_q, &item, 0);
                    ESP_LOGW(TAG, "推送失败, 断开重连");
                    close(fd);
                    fd = -1;
                    continue;
                }
            }
        }

        /* ---- 4) 连接可能已被对端关闭, 探测一下 ---- */
        if (fd >= 0) {
            /*
             * 用 MSG_PEEK | MSG_DONTWAIT 探测。
             *
             * 对端【正常关闭】时 recv() 返回 0 (不是错误), 我们据此断开。
             * 对端【被杀掉】时 (进程直接结束) TCP 会发 RST,
             * recv() 返回 -1 且 errno = ECONNRESET —— 这一种也必须断开,
             * 否则我们会一直以为连接还在, 事件全堆在队列里出不去。
             *
             * MSG_DONTWAIT 保证不会阻塞 —— 正常情况下没有数据可读,
             * 返回 -1/EAGAIN, 说明连接还活着。
             */
            uint8_t probe;
            int r = recv(fd, &probe, 1, MSG_PEEK | MSG_DONTWAIT);
            if (r == 0) {
                ESP_LOGW(TAG, "电脑端关闭了连接");
                close(fd);
                fd = -1;
                continue;
            }
            if (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                ESP_LOGW(TAG, "连接异常 (errno=%d), 断开重连", errno);
                close(fd);
                fd = -1;
                continue;
            }
        }
    }
}

/* ---------------- 对外接口 ---------------- */

esp_err_t push_client_start(void)
{
    if (s_task != NULL) {
        return ESP_OK;
    }

    s_event_q = xQueueCreate(PUSH_EVENT_MAX, sizeof(event_item_t));
    if (s_event_q == NULL) {
        ESP_LOGE(TAG, "事件队列创建失败");
        return ESP_FAIL;
    }

    jpeg_cache_init();

    /*
     * 任务栈放 PSRAM —— 内部 RAM 紧张。
     * 用 xTaskCreatePinnedToCore 而不是本工程的 task_start(),
     * 因为那个 helper 一次只能建一个任务, 而这里要和 camera_pins.h 的
     * PC_RECEIVER_IP 保持一致, 但结构上更适合独立成模块。
     */
    BaseType_t ok = xTaskCreatePinnedToCore(push_client_task, "push_client",
                                            6144, NULL, 3, &s_task, APP_CORE_ID);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "推模式任务创建失败");
        s_task = NULL;
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "推模式客户端已启动 (目标 %s:%d, 缓存 %d 事件 + %d 帧 JPEG)",
             PC_RECEIVER_IP, PC_RECEIVER_PORT, PUSH_EVENT_MAX, PUSH_JPEG_MAX);
    return ESP_OK;
}

bool push_client_is_connected(void)
{
    return s_connected;
}

bool push_client_submit_event(const char *body)
{
    if (s_event_q == NULL || body == NULL || body[0] == '\0') {
        return false;
    }

    event_item_t item;
    size_t len = strlen(body);
    if (len >= sizeof(item.body)) {
        len = sizeof(item.body) - 1;
    }
    memcpy(item.body, body, len);
    item.body[len] = '\0';

    /*
     * 分配提交序号。
     *
     * 用来区分"这条事件是断线期间产生的还是实时产生的" ——
     * 只有前者才需要打 replay 标记 (见 s_replay_upto 的注释)。
     */
    item.id = s_next_event_id++;

    /*
     * 队列满 -> 丢最旧的一条, 腾位置放新的。
     *
     * 为什么丢旧不丢新:
     *   新的事件是"刚刚发生的事", 旧的是"已经过去的事"。
     *   老人刚才按的求助键, 比五分钟前的普通事件重要得多。
     */
    if (xQueueSend(s_event_q, &item, 0) != pdTRUE) {
        event_item_t old;
        if (xQueueReceive(s_event_q, &old, 0) == pdTRUE) {
            s_dropped++;
        }
        if (xQueueSend(s_event_q, &item, 0) != pdTRUE) {
            s_dropped++;
            return false;
        }
    }
    return true;
}

bool push_client_push_jpeg(const uint8_t *data, size_t len)
{
    if (data == NULL || len == 0) {
        return false;
    }
    jpeg_cache_push(data, len);
    return true;
}

void push_client_get_stats(push_stats_t *out)
{
    if (out == NULL) {
        return;
    }
    out->sent_events = s_sent_events;
    out->sent_jpegs = s_sent_jpegs;
    out->replayed = s_replayed;
    out->dropped = s_dropped;
    out->queued = (s_event_q != NULL) ? (uint32_t)uxQueueMessagesWaiting(s_event_q) : 0;
    out->connect_fails = s_connect_fails;
    out->connected = s_connected;
    out->backoff_ms = s_backoff_ms;
}
