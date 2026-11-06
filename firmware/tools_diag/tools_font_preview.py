"""预览字库渲染效果, 确认中文字形清晰可读。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image

# 解析生成的 C 数组
import re

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "main", "lcd_font.c"), encoding="utf-8").read()

glyphs = {}
for m in re.finditer(
        r"static const uint16_t (_g_([0-9A-F]{4}))\[(\d+)\] = \{(.*?)\};",
        src, re.S):
    ident, code, h, body = m.group(1), int(m.group(2), 16), int(m.group(3)), m.group(4)
    vals = [int(x, 16) for x in re.findall(r"0x([0-9A-F]{4})", body)]
    glyphs[code] = (vals, h)

print(f"解析到 {len(glyphs)} 个字形")

def render(text, scale=4, pad=4):
    img = Image.new("L", (10, 10), 0)
    W = pad
    for ch in text:
        code = ord(ch)
        if code not in glyphs:
            continue
        W += 17
    H = 16 + pad * 2
    img = Image.new("L", (W, H), 0)
    x = pad
    for ch in text:
        code = ord(ch)
        if code not in glyphs:
            continue
        rows, h = glyphs[code]
        for yy in range(min(h, 16)):
            bits = rows[yy]
            for xx in range(16):
                if bits & (1 << (15 - xx)):
                    img.putpixel((x + xx, pad + yy), 255)
        x += 17
    return img.resize((W * scale, H * scale), Image.NEAREST)


tests = [
    "已通知家人",
    "家人正在看画面",
    "家人正在呼叫您",
    "该活动一下",
    "老人按了求助按钮",
    "求助已取消 2026-11-04",
]

imgs = [render(t) for t in tests]
W = max(i.width for i in imgs)
H = sum(i.height for i in imgs) + 10 * len(imgs)
sheet = Image.new("L", (W, H), 0)
y = 0
for i in imgs:
    sheet.paste(i, (0, y))
    y += i.height + 10

sheet.save(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "font_preview.png"))
print("已保存 font_preview.png")
for t in tests:
    print("  ", t)
