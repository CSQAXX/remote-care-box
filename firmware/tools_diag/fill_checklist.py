#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性脚本: 回填 docs/可行性验证环境checklist.md 的勾选状态。

规则:
  - 只处理以 "|" 开头、且第一列是 "N.M" 编号的表格行
  - 找到该行中【最后一个】 [ ] 或 [~] (即状态列), 换成 [x]
  - 日期写进状态列右边的空单元格 (如果这一行有日期列)
"""
import io
import re
import sys

PATH = "docs/可行性验证环境checklist.md"
DATE = "2026-11-04"

DONE = {
    "1.2": "2026-10-22", "1.3": "2026-10-22", "1.4": "2026-10-22",
    "1.5": "2026-10-22", "1.6": "2026-10-22", "1.7": "2026-10-22",
    "2.2": "2026-10-22", "2.3": "2026-10-22", "2.4": "2026-10-22",
    "2.5": "2026-10-22", "2.6": "2026-11-04", "2.7": "2026-10-22",
    "3.1": "2026-10-22", "3.2": "2026-10-22", "3.3": "2026-10-22",
    "3.4": "2026-10-22", "3.5": "2026-10-22", "3.6": "2026-10-22",
    "3.8": "2026-10-22", "3.9": "2026-10-22",
    "4.1": "2026-10-22", "4.2": "2026-10-22", "4.3": "2026-10-22",
    "4.4": "2026-10-22",
    "5.1": "2026-10-22", "5.2": "2026-10-22", "5.3": "2026-10-22",
    "5.4": "2026-10-22", "5.5": "2026-10-22", "5.6": "2026-11-04",
    "5.7": "2026-11-04", "5.8": "2026-11-04",
    "6.1": "2026-11-04", "6.2": "2026-11-04", "6.3": "2026-11-04",
    "6.4": "2026-11-04", "6.5": "2026-11-04",
    "7.3": "2026-11-04", "7.4": "2026-11-04",
}

NOTES = {
    "3.7": "本板不用正点原子官方 board config (BOX3 官方 board config 按 ES8388 配, 与本板实测的 ES8311+ES7210 冲突), 改为在 `camera_pins.h` 手工定义引脚",
    "5.8": "分辨率 320x240 达标; 帧率实测 7.5~7.8fps, 低于 10fps, 已评估接受",
    "7.3": "5555 被 Android 模拟器 (qemu-system-x86_64) 占用, 改用 5556",
}


def main() -> int:
    with io.open(PATH, "r", encoding="utf-8") as f:
        lines = f.read().split("\n")

    changed = 0
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        m = re.match(r"^\|\s*([0-9]+\.[0-9]+)\s*\|", line)
        if not m:
            continue
        num = m.group(1)
        if num not in DONE:
            continue

        # 找最后一个 [ ] / [~]
        matches = list(re.finditer(r"\[([ ~])\]", line))
        if not matches:
            continue
        last = matches[-1]
        new_line = line[:last.start()] + "[x]" + line[last.end():]

        # 日期: 状态列之后如果是空的 "| |" 就填进去
        # 找到状态列的位置, 再往后找下一个 "|", 看它后面是否为空单元格
        after = new_line[last.end():]
        # after 形如 " |  |" 或 " |"
        m2 = re.match(r"^(\s*\|\s*)(\|)?", after)
        if m2:
            gap = m2.group(1)
            # after = gap + 可能的一个 "|"
            rest = after[len(gap):]
            if rest.startswith("|"):
                # 状态列右边紧跟一个空单元格
                new_line = (new_line[:last.end()] + gap + f" {DONE[num]} |"
                           + after[len(gap) + 1:])
            else:
                new_line = new_line[:last.end()] + gap + f" {DONE[num]}" + after[len(gap):]

        if num in NOTES:
            new_line = new_line.rstrip() + f" — {NOTES[num]} "

        lines[i] = new_line
        changed += 1

    with io.open(PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"已更新 {changed} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
