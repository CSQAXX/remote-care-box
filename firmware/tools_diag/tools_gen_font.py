"""
生成 LCD 中文点阵字库 (GB2312 风格的 16x16 / 24x24 位图)。

背景:
  需求文档 §3.8 要求板载 LCD 显示中文提示 (防惊吓机制),
  比如「已通知家人」「家人正在看画面」「家人正在呼叫您」。
  但 ESP-IDF 没有内置中文字体, 而完整的中文字库有好几 MB, 塞不进 flash。

做法:
  只收录【实际用到的字符】, 用 Pillow 从系统字体渲染成位图,
  每个字符压缩成 1 bit/像素, 编译成 C 数组。

字体来源: Windows 自带 msyh.ttc (微软雅黑) / simhei.ttf (黑体)

用法:
  python tools_gen_font.py
  -> 生成 firmware/main/lcd_font.c 和 lcd_font.h
"""
from PIL import Image, ImageDraw, ImageFont
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_C = os.path.join(HERE, "main", "lcd_font.c")
OUT_H = os.path.join(HERE, "main", "lcd_font.h")

# 要收录的字符集 (覆盖所有会用到的中文提示语)
CHARS = (
    # 常用数字/字母/标点
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    " .,:;!?()[]{}<>+-*/=_%\"'#@&"
    # 求助相关
    "已通知家人取消求助按键按了老人通知您"
    # 家人状态
    "正在看画面呼叫您来看您了"
    # 内容干预
    "该活动一下起来走走喝水休息"
    # 状态/通用
    "无信号正常连接断开网络离线在线"
    # 其它可能用到的
    "声说话音量静音播放中停止继续等待"
)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
]


def load_font(size, index=0):
    for path in FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            return ImageFont.truetype(path, size, index=index)
        except Exception:
            continue
    raise RuntimeError("找不到可用的中文字体")


def render_glyph(ch, size, font):
    """把一个字符渲染成 size x size 的 1-bit 位图 (返回 list of row bitmasks)。"""
    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)
    # 居中绘制
    bbox = font.getbbox(ch)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (size - w) // 2 - bbox[0]
    y = (size - h) // 2 - bbox[1]
    draw.text((x, y), ch, font=font, fill=255)

    px = img.load()
    rows = []
    for yy in range(size):
        bits = 0
        for xx in range(size):
            if px[xx, yy] >= 128:
                bits |= (1 << (size - 1 - xx))
        rows.append(bits)
    return rows


def main():
    chars = []
    seen = set()
    for ch in CHARS:
        if ch in seen:
            continue
        seen.add(ch)
        code = ord(ch)
        if code < 0x80:
            # ASCII: 压缩成 8x16 (占 1 字节/行)
            chars.append((ch, 8, 16))
        else:
            chars.append((ch, 16, 16))

    # 16x16 字形
    font16 = load_font(16)
    font8 = load_font(16)  # ASCII 也用同一个渲染器, 但裁到 8 宽

    glyphs = {}
    for ch, w, h in chars:
        if w == 8:
            # ASCII: 渲染到 8x16 画布
            img = Image.new("L", (8, 16), 0)
            d = ImageDraw.Draw(img)
            d.text((0, 0), ch, font=font8, fill=255)
            px = img.load()
            rows = []
            for yy in range(16):
                bits = 0
                for xx in range(8):
                    if px[xx, yy] >= 128:
                        bits |= (1 << (7 - xx))
                rows.append(bits)
            glyphs[ch] = (8, 16, rows)
        else:
            glyphs[ch] = (16, 16, render_glyph(ch, 16, font16))

    # 生成 C 文件
    with open(OUT_C, "w", encoding="utf-8") as f:
        f.write("/*\n")
        f.write(" * LCD 中文点阵字库 —— 【自动生成, 请勿手改】\n")
        f.write(" *\n")
        f.write(" * 生成脚本: firmware/tools_gen_font.py\n")
        f.write(" * 来源字体: Windows 微软雅黑 / 黑体\n")
        f.write(" *\n")
        f.write(" * 为什么只收录用到的字:\n")
        f.write(" *   完整中文点阵库有好几 MB, 塞进 flash 太大,\n")
        f.write(" *   而需求文档 §3.8 的提示语是固定的几十个字, 全部收录只需几 KB。\n")
        f.write(" *\n")
        f.write(" * 格式: 每个字形是若干行, 每行一个 uint16_t,\n")
        f.write(" *       最高位 = 最左边像素 (1 = 亮点)。\n")
        f.write(" */\n\n")
        f.write('#include "lcd_font.h"\n\n')

        for ch, (w, h, rows) in glyphs.items():
            ident = f"_g_{ord(ch):04X}"
            f.write(f"/* {ch!r} */\n")
            f.write(f"static const uint16_t {ident}[{h}] = {{\n")
            for i in range(0, h, 8):
                chunk = rows[i:i + 8]
                f.write("    " + ", ".join(f"0x{r:04X}" for r in chunk) + ",\n")
            f.write("};\n\n")

        # 索引表
        f.write("const lcd_glyph_t lcd_font_glyphs[] = {\n")
        for ch, (w, h, rows) in glyphs.items():
            ident = f"_g_{ord(ch):04X}"
            f.write(f"    {{ 0x{ord(ch):04X}, {w}, {h}, {ident} }},\n")
        f.write("};\n\n")
        f.write(f"const int lcd_font_glyph_count = "
                f"sizeof(lcd_font_glyphs) / sizeof(lcd_font_glyphs[0]);\n")

    # 生成头文件
    with open(OUT_H, "w", encoding="utf-8") as f:
        f.write("/*\n")
        f.write(" * LCD 中文点阵字库 —— 【自动生成, 请勿手改】\n")
        f.write(" * 生成脚本: firmware/tools_gen_font.py\n")
        f.write(" */\n\n")
        f.write("#ifndef LCD_FONT_H\n")
        f.write("#define LCD_FONT_H\n\n")
        f.write("#include <stdint.h>\n\n")
        f.write("#ifdef __cplusplus\n")
        f.write('extern "C" {\n')
        f.write("#endif\n\n")
        f.write("typedef struct {\n")
        f.write("    uint16_t    code;   /* Unicode 码点 */\n")
        f.write("    uint8_t     width;  /* 像素宽 */\n")
        f.write("    uint8_t     height; /* 像素高 */\n")
        f.write("    const uint16_t *bits; /* 每行一个位掩码, 最高位=最左 */\n")
        f.write("} lcd_glyph_t;\n\n")
        f.write("extern const lcd_glyph_t lcd_font_glyphs[];\n")
        f.write("extern const int lcd_font_glyph_count;\n\n")
        f.write("#ifdef __cplusplus\n")
        f.write("}\n")
        f.write("#endif\n\n")
        f.write("#endif /* LCD_FONT_H */\n")

    total = sum(len(r) for (_, _, r) in glyphs.values()) * 2
    print(f"字库生成完成: {len(glyphs)} 个字符, {total} 字节")
    print(f"  -> {OUT_C}")
    print(f"  -> {OUT_H}")


if __name__ == "__main__":
    main()
