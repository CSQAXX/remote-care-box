/*
 * 带 CPU 让出点的 JPEG 编码器 —— 对外头文件
 *
 * 详见 jpeg_yield.cpp 的注释。
 * 背景: esp32-camera 的 fmt2jpg_cb() 编码时不让出 CPU (约 69ms),
 *       导致 core0 的 IDLE0 喂不了看门狗 -> 板子反复重启,
 *       同时饿死 LCD 任务 -> 屏幕卡死/闪花屏。
 */
#ifndef JPEG_YIELD_H
#define JPEG_YIELD_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 让出回调: 每处理 yield_every 行会被调用一次 */
typedef void (*jpeg_yield_cb)(void *arg);

/*
 * RGB565 (大端) -> JPEG, 编码过程中周期性让出 CPU。
 *
 * @param src         源图像, RGB565, 大端 (高位字节在前)
 * @param width       宽度
 * @param height      高度
 * @param quality     JPEG 质量 1-100
 * @param dst         输出缓冲
 * @param dst_cap     输出缓冲容量
 * @param out_len     实际写入字节数
 * @param ycb         让出回调 (可为 NULL)
 * @param yarg        让出回调参数
 * @param yield_every 每多少行让出一次 (0 = 不让出)
 * @return true 成功
 */
bool jpeg_encode_rgb565_yield(const uint8_t *src, int width, int height,
                              int quality, uint8_t *dst, size_t dst_cap,
                              size_t *out_len,
                              jpeg_yield_cb ycb, void *yarg,
                              int yield_every);

#ifdef __cplusplus
}
#endif

#endif /* JPEG_YIELD_H */
