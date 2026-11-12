# -*- coding: utf-8 -*-
"""
老人看护提示的配色回归检查。

用途:
    改动 lcd_prompt.c 之后, 确认老人那边仍然是「黑底黄字/红字」,
    没有被小电脑模块的白底黑字带跑。

做法:
    直接往串口发 `lcdprompt <help|watching|content 文案>` 命令
    (main.c 里的调试命令), 然后从日志里读它实际用的 fg/bg。

用法:
    python tools/lcd_prompt_check.py
"""
import io
import re
import sys
import time

import serial

PORT = "COM3"
BAUD = 115200

# 依次触发的提示: (串口命令, 期望的说明)
CASES = [
    ("lcdprompt help",      "求助 -> 应红字黑底 fg=0xF800 bg=0x0000"),
    ("lcdprompt watching",  "家人看画面 -> 应黄字黑底 fg=0xFFE0 bg=0x0000"),
    ("lcdprompt content 该活动一下", "干预文案 -> 应黄字黑底"),
]


def main():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.2)
    except Exception as e:
        print("打不开串口 %s: %s" % (PORT, e))
        print("提示: 先确认没有别的程序占着串口")
        return 1

    buf = io.StringIO()
    t_end = time.time() + 8
    while time.time() < t_end:
        data = ser.read(4096)
        if data:
            buf.write(data.decode("utf-8", "replace"))

    log = buf.getvalue()
    if "boot:" not in log and "rst:" not in log:
        print("(没抓到开机日志, 直接发命令试试)")

    for cmd, note in CASES:
        print(">>> %s   (%s)" % (cmd, note))
        # 连发 3 次: USB-Serial-JTAG 的 RX 偶尔会丢头几个字节
        for _ in range(3):
            ser.write((cmd + "\r\n").encode())
            ser.flush()
            time.sleep(0.4)
        t_end = time.time() + 3.0
        while time.time() < t_end:
            data = ser.read(4096)
            if data:
                buf.write(data.decode("utf-8", "replace"))

    ser.close()

    log = buf.getvalue()
    print("---- 实际画横幅的配色 ----")
    found = False
    for line in log.splitlines():
        if "画横幅" in line or "显示提示" in line:
            m = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
            print("   ", m)
            found = True
    if not found:
        print("    (没有横幅日志, 可能命令没生效)")

    print("---- 结论 ----")
    for line in log.splitlines():
        if "画横幅" in line:
            m = re.search(r"fg=0x([0-9A-Fa-f]{4}) bg=0x([0-9A-Fa-f]{4})", line)
            if not m:
                continue
            fg, bg = m.group(1).upper(), m.group(2).upper()
            if bg == "0000" and fg in ("FFE0", "F800"):
                print("   OK  老人提示仍是黑底黄/红字 (fg=0x%s bg=0x%s)" % (fg, bg))
            else:
                print("   注意 配色变成 fg=0x%s bg=0x%s (老人那边本该是 0xFFE0/0xF800 + 0x0000)"
                      % (fg, bg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
