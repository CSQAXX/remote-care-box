/*
 * 带 CPU 让出点的 JPEG 编码器
 *
 * 为什么需要它:
 *   esp32-camera 自带的 fmt2jpg_cb() 是个黑盒, 它在 convert_image() 里
 *   一口气把 240 行全部编码完, 中间【不让出 CPU】。
 *   一次编码要 ~70ms, 期间编码任务独占 CPU, 优先级更低的 LCD 刷新
 *   被活活饿死 -> 屏幕卡住 -> 看起来在一闪一闪。
 *
 *   这个文件复刻了 to_jpg.cpp 的 convert_image() 逻辑, 但每处理
 *   N 行就调用一次 yield 回调, 让出 CPU 给 LCD / Wi-Fi / 系统任务。
 *
 * 编码结果与 fmt2jpg_cb 完全一致 (同样的 jpge 参数, 同样的转换),
 * 只是多了让出点。
 */

#include <stddef.h>
#include <stdint.h>

#include "jpge.h"

extern "C" {

/*
 * 让出回调: 由调用方提供, 通常是 taskYIELD() 或 vTaskDelay(1)。
 * 每处理 yield_every 行会被调用一次。
 */
typedef void (*jpeg_yield_cb)(void *arg);

/*
 * 把 src (RGB565 大端) 编码成 JPEG, 输出到 dst。
 *
 * @param src        源图像, RGB565, 大端 (高位字节在前), 与摄像头帧缓冲一致
 * @param width      宽度
 * @param height     高度
 * @param quality    JPEG 质量 1-100, 越大越清晰
 * @param dst        输出缓冲
 * @param dst_cap    输出缓冲容量
 * @param out_len    实际写入字节数
 * @param ycb        让出回调
 * @param yarg       让出回调参数
 * @param yield_every 每多少行让出一次 (0 = 不让出)
 * @return true 成功
 */
bool jpeg_encode_rgb565_yield(const uint8_t *src, int width, int height,
                               int quality, uint8_t *dst, size_t dst_cap,
                               size_t *out_len,
                               jpeg_yield_cb ycb, void *yarg,
                               int yield_every);

} /* extern "C" */

/* ---------------- C++ 实现 ---------------- */

namespace {

/* 把 RGB565 一行转成 RGB888 (大端源) */
static inline void convert_row_rgb565_to_rgb888(const uint8_t *src, uint8_t *dst, int width)
{
    for (int i = 0; i < width; i++) {
        const uint8_t hi = src[i * 2];
        const uint8_t lo = src[i * 2 + 1];

        const uint8_t r = (uint8_t)(hi & 0xF8);
        const uint8_t g = (uint8_t)(((hi & 0x07) << 5) | ((lo & 0xE0) >> 3));
        const uint8_t b = (uint8_t)((lo & 0x1F) << 3);

        dst[i * 3 + 0] = r;
        dst[i * 3 + 1] = g;
        dst[i * 3 + 2] = b;
    }
}

/* 输出流: 直接写进目标缓冲, 满了就停 */
class buffer_stream : public jpge::output_stream {
public:
    buffer_stream(uint8_t *buf, size_t cap) : m_buf(buf), m_cap(cap), m_index(0) { }
    virtual ~buffer_stream() { }

    virtual bool put_buf(const void *data, int len)
    {
        if (len < 0) {
            return true;
        }
        if (m_index + (size_t)len > m_cap) {
            size_t room = m_cap - m_index;
            if (room > 0) {
                memcpy(m_buf + m_index, data, room);
                m_index += room;
            }
            return false;   /* 满了, 告诉编码器停止 */
        }
        memcpy(m_buf + m_index, data, (size_t)len);
        m_index += (size_t)len;
        return true;
    }

    virtual jpge::uint get_size() const { return (jpge::uint)m_index; }

    size_t written() const { return m_index; }

private:
    uint8_t *m_buf;
    size_t   m_cap;
    size_t   m_index;
};

} /* anonymous namespace */

extern "C" bool jpeg_encode_rgb565_yield(const uint8_t *src, int width, int height,
                                        int quality, uint8_t *dst, size_t dst_cap,
                                        size_t *out_len,
                                        jpeg_yield_cb ycb, void *yarg,
                                        int yield_every)
{
    if (src == NULL || dst == NULL || out_len == NULL || width <= 0 || height <= 0) {
        return false;
    }
    if (quality < 1) {
        quality = 1;
    } else if (quality > 100) {
        quality = 100;
    }
    if (yield_every <= 0) {
        yield_every = height + 1;   /* 相当于不让出 */
    }

    jpge::params comp_params;
    comp_params.m_subsampling = jpge::H2V2;   /* 和 to_jpg.cpp 默认一致 */
    comp_params.m_quality = quality;

    buffer_stream stream(dst, dst_cap);

    jpge::jpeg_encoder enc;
    if (!enc.init(&stream, width, height, 3, comp_params)) {
        return false;
    }

    /* 一行 RGB888 的缓冲 (320*3 = 960 字节), 放内部 RAM 避免 PSRAM 访问 */
    uint8_t line[320 * 3];
    if (width * 3 > (int)sizeof(line)) {
        enc.deinit();
        return false;
    }

    bool ok = true;
    for (int y = 0; y < height; y++) {
        convert_row_rgb565_to_rgb888(src + (size_t)y * width * 2, line, width);

        if (!enc.process_scanline(line)) {
            ok = false;
            break;
        }

        /*
         * 让出 CPU。
         * 编码是纯计算, 不让出就会把别的任务饿死。
         * 每 yield_every 行让一次, 在"及时刷新"和"编码效率"之间取平衡。
         */
        if (ycb != NULL && ((y + 1) % yield_every) == 0) {
            ycb(yarg);
        }
    }

    if (ok) {
        ok = enc.process_scanline(NULL);   /* 收尾 */
    }

    enc.deinit();

    *out_len = stream.written();
    return ok;
}
