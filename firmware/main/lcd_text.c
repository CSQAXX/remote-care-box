/*
 * LCD 文本渲染 (中文点阵字库) —— 实现
 *
 * 见 lcd_text.h 的说明。
 *
 * 渲染策略:
 *   1. 把 UTF-8 文本解成 Unicode 码点;
 *   2. 每个字形查字库, 找不到就跳过 (空一个字符宽);
 *   3. 逐行组装成 RGB565 位图, 每 20 行一批发给 LCD。
 *
 * 为什么每行单独调用 lcd_fill_rect / draw_bitmap:
 *   - 直接复用 lcd_st7789.c 里已经调好的双缓冲 + ISR 安全信号量路径,
 *     不用再写一份 SPI 逻辑 (少一个出错的地方);
 *   - 但那样会每个字都发一次 SPI, 太慢。
 *   所以折中: 把一整行文本先画到【内部 RAM 的行缓冲】,
 *   再一次性 draw_bitmap 送出去。一行 320 像素 = 640 字节, 很便宜。
 */

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_heap_caps.h"

#include "lcd_st7789.h"
#include "lcd_text.h"
#include "lcd_font.h"
#include "camera_pins.h"   /* LCD_WIDTH / LCD_HEIGHT */

static const char *TAG = "lcdtext";

/* ---------------- UTF-8 解码 ---------------- */

/*
 * 从 UTF-8 字符串里解出下一个码点, 前进 p。
 * 返回 0 表示字符串结束。
 *
 * UTF-8 编码规则 (简化):
 *   0xxxxxxx                 -> 1 字节 (ASCII)
 *   110xxxxx 10xxxxxx        -> 2 字节
 *   1110xxxx 10xxxxxx 10xxxxxx -> 3 字节 (中文)
 *   11110xxx ...             -> 4 字节 (emoji 等)
 */
static uint32_t utf8_next(const char **p)
{
    const uint8_t *s = (const uint8_t *)*p;
    uint8_t c = *s;

    if (c == 0) {
        return 0;
    }
    if (c < 0x80) {
        *p += 1;
        return c;
    }
    if ((c & 0xE0) == 0xC0 && (s[1] & 0xC0) == 0x80) {
        uint32_t cp = ((uint32_t)(c & 0x1F) << 6) | (s[1] & 0x3F);
        *p += 2;
        return cp;
    }
    if ((c & 0xF0) == 0xE0 && (s[1] & 0xC0) == 0x80 && (s[2] & 0xC0) == 0x80) {
        uint32_t cp = ((uint32_t)(c & 0x0F) << 12) |
                      ((uint32_t)(s[1] & 0x3F) << 6) |
                      (s[2] & 0x3F);
        *p += 3;
        return cp;
    }
    if ((c & 0xF8) == 0xF0 && (s[1] & 0xC0) == 0x80 &&
        (s[2] & 0xC0) == 0x80 && (s[3] & 0xC0) == 0x80) {
        uint32_t cp = ((uint32_t)(c & 0x07) << 18) |
                      ((uint32_t)(s[1] & 0x3F) << 12) |
                      ((uint32_t)(s[2] & 0x3F) << 6) |
                      (s[3] & 0x3F);
        *p += 4;
        return cp;
    }

    /* 非法字节: 跳过 1 个 */
    *p += 1;
    return 0xFFFD;
}

/* ---------------- 字库查找 ---------------- */

static const lcd_glyph_t *glyph_find(uint32_t cp)
{
    for (int i = 0; i < lcd_font_glyph_count; i++) {
        if (lcd_font_glyphs[i].code == cp) {
            return &lcd_font_glyphs[i];
        }
    }
    return NULL;
}

/* ---------------- 尺寸计算 ---------------- */

int lcd_text_height(int scale)
{
    if (scale < 1) {
        scale = 1;
    }
    return LCD_FONT_HEIGHT * scale;
}

int lcd_text_width(const char *text, int scale)
{
    if (text == NULL) {
        return 0;
    }
    if (scale < 1) {
        scale = 1;
    }

    int w = 0;
    const char *p = text;
    while (*p) {
        uint32_t cp = utf8_next(&p);
        if (cp == 0) {
            break;
        }
        const lcd_glyph_t *g = glyph_find(cp);
        w += ((g ? g->width : LCD_CHAR_WIDTH - 1) + 1) * scale;
    }
    return w;
}

/* ---------------- 绘制 ---------------- */

/*
 * 把一整段文本渲染到内部 RAM 的 RGB565 缓冲 (w x h)。
 *
 * @param buf   目标缓冲, 至少 w*h*2 字节, 必须是内部 RAM (LCD DMA 要用)
 * @param max_w 可用宽度 (像素)
 */
static void render_text_to_buf(uint16_t *buf, int max_w, int max_h,
                               const char *text, uint16_t color, uint16_t bg,
                               int scale)
{
    if (scale < 1) {
        scale = 1;
    }

    /* 先填背景 */
    for (int i = 0; i < max_w * max_h; i++) {
        buf[i] = bg;
    }

    int pen_x = 0;
    const char *p = text;
    while (*p) {
        uint32_t cp = utf8_next(&p);
        if (cp == 0) {
            break;
        }

        const lcd_glyph_t *g = glyph_find(cp);
        if (g == NULL) {
            /* 字库里没有的字: 空一个字符宽, 不画任何东西 */
            pen_x += LCD_CHAR_WIDTH * scale;
            continue;
        }

        for (int gy = 0; gy < g->height; gy++) {
            int py = gy * scale;
            if (py >= max_h) {
                break;
            }
            uint16_t rowbits = g->bits[gy];
            if (rowbits == 0) {
                continue;
            }
            for (int gx = 0; gx < g->width; gx++) {
                if (!(rowbits & (1 << (g->width - 1 - gx)))) {
                    continue;
                }
                /* 一个点放大成 scale x scale 的方块 */
                for (int sy = 0; sy < scale; sy++) {
                    int yy = py + sy;
                    if (yy >= max_h) {
                        break;
                    }
                    uint16_t *drow = &buf[yy * max_w];
                    for (int sx = 0; sx < scale; sx++) {
                        int xx = pen_x + gx * scale + sx;
                        if (xx >= max_w) {
                            break;
                        }
                        drow[xx] = color;
                    }
                }
            }
        }
        pen_x += (g->width + 1) * scale;
        if (pen_x >= max_w) {
            break;
        }
    }
}

int lcd_draw_text(int x, int y, const char *text, uint16_t color,
                  uint16_t bg, int scale)
{
    if (text == NULL || text[0] == '\0') {
        return 0;
    }
    if (scale < 1) {
        scale = 1;
    }

    int th = lcd_text_height(scale);
    int tw = lcd_text_width(text, scale);

    /* 裁到屏幕内 */
    if (x < 0) { tw += x; x = 0; }
    if (y < 0) { th += y; y = 0; }
    if (x + tw > LCD_WIDTH) { tw = LCD_WIDTH - x; }
    if (y + th > LCD_HEIGHT) { th = LCD_HEIGHT - y; }
    if (tw <= 0 || th <= 0) {
        return 0;
    }

    /*
     * 文本行缓冲放 PSRAM。
     *
     * 【2026-11-05 修复黑屏】之前放内部 RAM, 但这块板子内部 RAM 极紧张
     * (开机实测只剩 16983 字节), 而屏幕提示的横幅可能接近整屏宽
     * (320 x 40 x 2 = 25600 字节) -> 申请失败 -> 提示画不出来。
     *
     * 为什么放 PSRAM 没问题:
     *   lcd_draw_rgb565() 会把源【逐行拷贝】到内部 RAM 的常驻 DMA 缓冲
     *   再发出去, SPI DMA 读的始终是内部 RAM。
     *   所以源缓冲放哪都行, 它只是"被 CPU 读"的普通内存。
     */
    uint16_t *buf = heap_caps_malloc((size_t)tw * th * sizeof(uint16_t),
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buf == NULL) {
        ESP_LOGE(TAG, "文本行缓冲分配失败 (%d 字节, PSRAM)", tw * th * 2);
        return 0;
    }

    render_text_to_buf(buf, tw, th, text, color, bg, scale);

    /*
     * swap_bytes = false: 我们写进去的是主机字节序的 RGB565,
     * 而 lcd_draw_rgb565 的 swap_bytes=true 是给"大端源数据"用的。
     * 这里我们自己组装, 已经是正确的小端 RGB565, 所以 false。
     */
    esp_err_t err = lcd_draw_rgb565(buf, tw * 2, x, y, tw, th, false, true);
    free(buf);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "画文本失败: %s", esp_err_to_name(err));
        return 0;
    }
    return tw;
}

int lcd_draw_text_centered(int box_x, int box_y, int box_w, int box_h,
                           const char *text, uint16_t color, uint16_t bg,
                           int scale)
{
    if (text == NULL || box_w <= 0) {
        return 0;
    }
    if (scale < 1) {
        scale = 1;
    }

    int tw = lcd_text_width(text, scale);
    int th = lcd_text_height(scale);

    int x = box_x + (box_w - tw) / 2;
    int y = box_y + (box_h - th) / 2;

    /* 文本比框宽时从左边截断, 不越界 */
    if (x < box_x) {
        x = box_x;
    }
    if (y < box_y) {
        y = box_y;
    }
    return lcd_draw_text(x, y, text, color, bg, scale);
}
