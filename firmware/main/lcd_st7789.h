/*
 * 板载 ST7789 LCD 驱动 (正点原子 ATK_DNESP32S3_BOX3)
 *
 * 硬件:
 *   - 320x240, RGB565, SPI2, mode 0, 60MHz
 *   - SCLK=15  MOSI=16  MISO=17  DC=48  CS=47
 *   - 背光不是 GPIO, 走 AW9523B 的 XIO8 (低电平点亮), 在 aw9523b / main.c 里控制
 *
 * 说明:
 *   本文件不画复杂界面, 只做两件事:
 *     1) 把面板初始化起来 (否则屏幕一直黑)
 *     2) 提供一个"纯色刷屏"接口, 用来验证屏幕通没通
 *   后续要做 UI 再上 LVGL。
 */

#ifndef LCD_ST7789_H
#define LCD_ST7789_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 初始化 SPI 总线与 ST7789 面板
 * @return ESP_OK 成功
 */
esp_err_t lcd_init(void);

/**
 * @brief 全屏刷一个纯色 (RGB565)
 */
esp_err_t lcd_fill_screen(uint16_t color);

/**
 * @brief 往指定矩形区域刷纯色
 */
esp_err_t lcd_fill_rect(int x, int y, int w, int h, uint16_t color);

/**
 * @brief 把一张 RGB565 图像画到屏幕上
 *
 * @param src          源数据指针 (RGB565)
 * @param src_stride   源数据每行的字节数
 * @param x            屏幕目标 X
 * @param y            屏幕目标 Y
 * @param w            图像宽 (像素)
 * @param h            图像高 (像素)
 * @param swap_bytes   true = 源数据是大端 (需要逐像素交换高低字节才能显示正确颜色)
 */
esp_err_t lcd_draw_rgb565(const void *src, int src_stride, int x, int y, int w, int h,
                           bool swap_bytes);

/**
 * @brief 画测试图样 (不依赖摄像头), 用来单独验证 LCD 传输链路
 *
 * 依次绘制: 8 条标准 RGB565 色条 + 8 级灰阶 + 网格。
 * 如果这个图样稳定不闪, 说明 LCD/SPI 链路正常, 问题在摄像头数据侧;
 * 如果这个图样也闪, 说明问题在 LCD/SPI 链路本身。
 */
esp_err_t lcd_draw_test_pattern(void);

/**
 * @brief 用一张【合成】的 PSRAM 图像测试 lcd_draw_rgb565 路径
 *
 * 用途: 进一步区分 "draw_rgb565 代码路径有问题" 和 "摄像头帧数据有问题"。
 * 缓冲区在 PSRAM (和摄像头帧缓冲同处), 内容是程序生成的渐变图,
 * 完全不涉及摄像头。
 *
 * @param swap_bytes   true = 按"源是大端"处理 (和摄像头当前一致)
 */
esp_err_t lcd_draw_synthetic_image(bool swap_bytes);

/**
 * @brief 摄像头帧缓冲诊断: 把源缓冲和"拷到内部 RAM 的副本"分别画到上下半屏
 *
 * 用途: 不用人眼看也能定位问题。
 *   - 上半屏 = 直接画 PSRAM 里的摄像头原始帧缓冲
 *   - 下半屏 = 先把整帧拷到内部 RAM, 再画那份副本
 *
 * 判读:
 *   - 上半屏花 / 下半屏干净 -> 问题在"从 PSRAM 读摄像头帧缓冲"这一步
 *                             (典型: DMA 写 PSRAM 后 cache 没同步)
 *   - 两半都花               -> 摄像头帧数据本身就是垃圾
 *                             (典型: DMA 没把整帧搬完 / 传感器配置错)
 *   - 两半都干净             -> 帧数据没问题, 之前的花屏另有原因
 *
 * @param src        摄像头帧缓冲 (PSRAM)
 * @param src_stride 每行字节数
 * @param w,h        图像尺寸
 * @param swap_bytes 画下半屏副本时是否交换字节
 * @param copy_buf   内部 RAM 副本缓冲 (w*h*2 字节)
 */
esp_err_t lcd_draw_frame_diagnostic(const void *src, int src_stride, int w, int h,
                                     bool swap_bytes, uint8_t *copy_buf);

/**
 * @brief 四象限花屏定位图 (见 lcd_st7789.c 里的注释)
 *
 *  +----------------+----------------+
 *  | 左上: PSRAM源   | 右上: 交换字节  |
 *  | 左下: 内部RAM   | 右下: 合成渐变  |
 *  +----------------+----------------+
 *
 * 判读:
 *   - 右下 (合成图) 也花  -> LCD/SPI 链路本身坏了
 *   - 右下正常, 左上/右上花 -> 摄像头帧数据是垃圾
 *   - 全都正常           -> 之前的"花"是显示时序问题
 */
esp_err_t lcd_draw_quad_diagnostic(const void *src, int src_stride, int w, int h,
                                   bool swap_bytes, uint8_t *copy_buf);

#ifdef __cplusplus
}
#endif

#endif /* LCD_ST7789_H */
