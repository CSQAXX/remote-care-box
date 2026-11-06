/*
 * LCD 文本渲染 (中文点阵字库)
 *
 * 用途:
 *   需求文档 §3.8「求助与屏幕提示」的核心 —— 屏幕提示是【防惊吓机制】:
 *   老人在不知情下被看到/被听到容易恐慌, 所以所有远程行为都要在设备端
 *   有可见提示。提示文案是中文, 所以需要点阵字库。
 *
 * 为什么自己写而不用 LVGL:
 *   1. 我们只需要"画一行字", LVGL 那一套 (布局/主题/输入设备) 太重;
 *   2. LVGL 要占大量内部 RAM (ESP32-S3 上很紧张, 之前音频排查过);
 *   3. 字库只有 4.7KB (只收录实际用到的字), LVGL 配字体反而更大。
 *
 * 字库来源: firmware/tools_gen_font.py 自动生成 (lcd_font.c)
 *   来源字体: Windows 微软雅黑 / 黑体
 *
 * 坐标约定:
 *   屏幕原点 (0,0) 在左上角, x 向右, y 向下。
 */

#ifndef LCD_TEXT_H
#define LCD_TEXT_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 文本颜色 (RGB565) */
#define LCD_TEXT_BLACK   0x0000
#define LCD_TEXT_WHITE   0xFFFF
#define LCD_TEXT_YELLOW  0xFFE0
#define LCD_TEXT_RED     0xF800
#define LCD_TEXT_GREEN   0x07E0
#define LCD_TEXT_BLUE    0x001F
#define LCD_TEXT_CYAN    0x07FF

/** 默认字体高度 (像素) */
#define LCD_FONT_HEIGHT   16
/** 默认字符宽度 (像素, 含 1 像素间隔) */
#define LCD_CHAR_WIDTH    17

/**
 * @brief 画一段 UTF-8 文本
 *
 * @param x,y     左上角坐标 (像素)
 * @param text    UTF-8 字符串 (遇到字库里没有的字会跳过)
 * @param color   字色 (RGB565)
 * @param bg      背景色 (RGB565)
 * @param scale   放大倍数 (1=原始 16x16, 2=32x32)
 * @return 实际画出的宽度 (像素)
 */
int lcd_draw_text(int x, int y, const char *text, uint16_t color,
                  uint16_t bg, int scale);

/**
 * @brief 居中画一段文本 (在给定宽度内水平居中)
 *
 * @param box_x,box_y  区域左上角
 * @param box_w,box_h  区域宽高
 */
int lcd_draw_text_centered(int box_x, int box_y, int box_w, int box_h,
                           const char *text, uint16_t color, uint16_t bg,
                           int scale);

/**
 * @brief 计算一段文本的像素宽度 (不实际绘制)
 */
int lcd_text_width(const char *text, int scale);

/**
 * @brief 测量文本高度 (像素)
 */
int lcd_text_height(int scale);

#ifdef __cplusplus
}
#endif

#endif /* LCD_TEXT_H */
