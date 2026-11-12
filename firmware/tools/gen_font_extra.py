#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
给 LCD 点阵字库补字。

背景
----
lcd_font.c 里只有 149 个字形，汉字仅 57 个（都是老人看护那套提示用到的字）。
lcd_text.c 渲染时遇到字库外的字是【直接跳过、留空】的，所以「小电脑」模块的
提示在屏幕上几乎一个字都画不出来 —— 看着就是闪一下黑色横幅。

本脚本【只追加】缺失的字形，一个字节都不改已有的条目，因此老人看护的显示
完全不受影响。

用法
----
    python tools/gen_font_extra.py            # 补齐内置字表
    python tools/gen_font_extra.py --dry-run  # 只看要加哪些字
"""

import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    r"C:/Windows/Fonts/simhei.ttf",   # 黑体：16px 下最清楚
    r"C:/Windows/Fonts/simsun.ttc",
]

FONT_C = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "main", "lcd_font.c")

# ---- 要补的字 --------------------------------------------------------------
# 1) 「小电脑」模块自己的固定提示用字
PC_PROMPT = (
    "小电脑听着呢收到办没清再遍送去内存够这轮跳过配口令麦克风忙喊次"
    "回话连上下来先等"
)

# 2) 电脑回话里的高频字（屏幕一行 17 个字，够看懂大意就行）
COMMON = (
    "你我好是不公会要相请帮打开关微信客户窗稍候经成功失败错误问题"
    "天气今明点分钟现什么怎样可以需完处理等马给记提醒设置添加删除"
    "查询搜索文件夹音乐视频照片消息新闻时间日期星期谢对起抱歉知道"
    "白得做用说里那些也都还只就能很多少大高快慢前后左右"
    "您已经正在还要先后来去出进走坐看想问找拿放买卖付钱块元"
)

# 3) 中文标点（电脑回话里大量出现，缺了就是一片空白）
PUNCT = "，。？！：、（）「」…·【】《》—～“”；﹑"

# 4) 通用高频汉字 —— 电脑回话千变万化，铺足底子，别每次都缺字
FREQ = (
    "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要"
    "她出也得里后自以会家可下而过天去能对小多然于心学么之都好看起发当"
    "没成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手"
    "又行意动方期它头经长儿回位分爱老因很给名法间斯知世什两次使身者被"
    "高已亲其进此话常与活正感见明问力理点文几定本公特做外孩相西果走将"
    "月十实向声车全信重三机工物气每并别真打太新比才便夫再书部水像眼等"
    "体却加电主界门利海受听表德少克代员许先口由死安写性马光白或住难望"
    "教命花结乐色更拉东神记处让母父应直字场平报友关放至张认接告入笑内"
    "英军候民岁往何度山觉路带万男边风解叫任金快原满目该未始空江语飞非"
    "突支题建南志身队商算质团导争展格效存求必受思切院查受况品住界千"
    "启转交执端计确效涉险尚才具应装告例超仍整直操逆尝试其统括捷式录"
    "注册检否供径希诉排早半根据某务吗演练将型桌览器或切作未认若原运"
)

# 5) 自动从电脑端日志里收集「实际出现过但字库没有」的字
LOG_CANDIDATES = [
    r"D:/Software/local-dev/logs/iot.log",
]


def collect_from_logs(have):
    """从 iot_bridge 的审计日志里把实际回过的字捞出来，补字库用。"""
    import collections
    import json

    cnt = collections.Counter()
    for path in LOG_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    for k in ("reply", "text"):
                        for ch in str(o.get(k) or ""):
                            if ord(ch) > 0x7F and ord(ch) not in have:
                                cnt[ch] += 1
        except OSError:
            continue
    return [c for c, _ in cnt.most_common()]


EXTRA = PC_PROMPT + COMMON + PUNCT + FREQ


def load_existing(text):
    """返回 (已有码点集合, glyphs 数组体, glyphs 数组起始下标, 结尾下标)"""
    m = re.search(r"const lcd_glyph_t lcd_font_glyphs\[\] = \{(.*?)\n\};", text, re.S)
    if not m:
        raise SystemExit("在 lcd_font.c 里找不到 lcd_font_glyphs 数组")
    body = m.group(1)
    codes = {int(c, 16) for c in re.findall(r"\{\s*0x([0-9A-Fa-f]{4})", body)}
    return codes, body, m.start(1), m.end(1)


def render_glyph(ch, font):
    """渲染一个字符到 16x16 点阵，返回 16 个 uint16（最高位=最左）"""
    img = Image.new("1", (16, 16), 0)
    d = ImageDraw.Draw(img)
    d.text((0, 0), ch, font=font, fill=1)
    rows = []
    for y in range(16):
        bits = 0
        for x in range(16):
            if img.getpixel((x, y)):
                bits |= 1 << (15 - x)
        rows.append(bits)
    return rows


def main():
    dry = "--dry-run" in sys.argv

    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if font_path is None:
        raise SystemExit("找不到可用的中文字体")
    font = ImageFont.truetype(font_path, 16)

    with open(FONT_C, "r", encoding="utf-8") as f:
        text = f.read()

    have, body, body_start, body_end = load_existing(text)

    # 内置字表 + 从真实回话日志里捞出来的字
    from_logs = collect_from_logs(have)
    if from_logs:
        print("从电脑端日志额外收集到 %d 个字: %s" % (len(from_logs), "".join(from_logs)))
    full = EXTRA + "".join(from_logs)

    # 去重、保序、跳过字库里已有的
    todo = []
    seen = set()
    for ch in full:
        cp = ord(ch)
        if cp in have or cp in seen:
            continue
        seen.add(cp)
        todo.append(ch)

    print("字库现有 %d 个字形" % len(have))
    print("需要补 %d 个: %s" % (len(todo), "".join(todo)))
    if dry or not todo:
        return

    # 生成字模定义（必须放在 glyphs 数组【之前】，C 语言要求先声明后使用）
    defs = []
    entries = []
    for ch in todo:
        cp = ord(ch)
        rows = render_glyph(ch, font)
        name = "_g_add_%04X" % cp
        vals = ", ".join("0x%04X" % r for r in rows)
        defs.append("/* '%s' （补充） */\nstatic const uint16_t %s[16] = {\n    %s,\n};\n"
                    % (ch, name, vals))
        entries.append("    { 0x%04X, 16, 16, %s }," % (cp, name))

    anchor = "const lcd_glyph_t lcd_font_glyphs[] = {"
    idx = text.index(anchor)

    new_text = (text[:idx]
                + "\n/* ===== 以下字形由 tools/gen_font_extra.py 补充, 供「小电脑」模块使用 ===== */\n"
                + "\n".join(defs)
                + "\n"
                + text[idx:])

    # 重新定位 glyphs 数组体（插入后索引变了），在结尾前追加条目
    _, body2, bstart, bend = load_existing(new_text)
    new_text = new_text[:bend] + "\n" + "\n".join(entries) + new_text[bend:]

    with open(FONT_C, "w", encoding="utf-8") as f:
        f.write(new_text)

    print("已写入 %s，新增 %d 个字形" % (FONT_C, len(todo)))


if __name__ == "__main__":
    main()
