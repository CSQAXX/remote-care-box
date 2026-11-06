/*
 * LCD 中文点阵字库 —— 【自动生成, 请勿手改】
 * 生成脚本: firmware/tools_gen_font.py
 */

#ifndef LCD_FONT_H
#define LCD_FONT_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint16_t    code;   /* Unicode 码点 */
    uint8_t     width;  /* 像素宽 */
    uint8_t     height; /* 像素高 */
    const uint16_t *bits; /* 每行一个位掩码, 最高位=最左 */
} lcd_glyph_t;

extern const lcd_glyph_t lcd_font_glyphs[];
extern const int lcd_font_glyph_count;

#ifdef __cplusplus
}
#endif

#endif /* LCD_FONT_H */
