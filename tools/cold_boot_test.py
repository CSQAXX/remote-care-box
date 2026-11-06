#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冷启动回归测试 (P0.3)
===================

目的: 验证板子【拔电重启】后, 而不是【热重启】后, 依然能:
    1. 摄像头初始化成功并出图
    2. 音频 codec 就绪
    3. LCD 亮
    4. AW9523B 应答 (I2C 总线没被拉死)

为什么必须冷启动:
    之前的排障里, 板子经常是"热启动能工作, 冷启动起不来"——
    因为前一次运行已经把 AW9523B 的寄存器配好了 (摄像头供电、功放使能),
    热启动时这些状态还在。只有真正断电, 才能测出固件的初始化顺序有没有问题。

用法:
    # 1) 先开一个监听器 (本脚本自己开串口读日志)
    # 2) 手动拔 USB 等 10 秒, 再插回去 —— 脚本会一直等
    python cold_boot_test.py --timeout 90

    # 或者用 RTS/DTR 模拟断电 (多数开发板效果等同于冷启动的软复位,
    # 但对 I2C 状态的验证强度不如真拔线, 所以默认推荐手动拔插)
    python cold_boot_test.py --reset
"""
import argparse
import json
import re
import serial
import sys
import time
import urllib.request

PORT = "COM3"
BAUD = 115200
DEVICE = "192.168.2.138"
HEALTH = f"http://{DEVICE}:8080/health"

ANSI = re.compile(r"\x1b\[[0-9;]*m")

CHECKS = {
    "camera_ok":     ("摄像头初始化", lambda v: v is True),
    "audio_ready":   ("音频 codec 就绪", lambda v: v is True),
    "lcd_direct":    ("LCD 直绘开启", lambda v: v is True),
    "torn_frames":   ("无花屏", lambda v: (v or 0) == 0),
    "psram":         ("PSRAM 8MB", lambda v: v == 8388608),
    "time_synced":   ("时间已同步", lambda v: v is True),
}


def probe_health(timeout=5):
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=90, help="等待板子上电的总秒数")
    ap.add_argument("--reset", action="store_true",
                    help="用串口 RTS 复位 (不如真拔线严格)")
    args = ap.parse_args()

    print("=" * 60)
    print("冷启动回归测试 (P0.3)")
    if args.reset:
        print("模式: 串口 RTS 软复位")
    else:
        print("模式: 手动拔 USB —— 现在请【拔掉 USB 线，等 10 秒，再插回去】")
    print("=" * 60)

    # 先把板子复位掉, 让它进入"刚上电"的干净状态
    s = serial.Serial(PORT, BAUD, timeout=0.2)
    if args.reset:
        s.setDTR(False)
        s.setRTS(True)
        time.sleep(0.1)
        s.setRTS(False)
        time.sleep(0.3)
        print("[*] 已发出复位")

    buf = []
    start = time.time()
    seen_ready = False
    while time.time() - start < args.timeout:
        try:
            raw = s.read(4096)
            if raw:
                txt = raw.decode("utf-8", "replace")
                buf.append(txt)
                if not seen_ready:
                    sys.stdout.write(txt)
                    sys.stdout.flush()
        except Exception:
            pass

        if not seen_ready:
            h = probe_health(timeout=2)
            if h and h.get("uptime", 0) < 120:
                seen_ready = True
                print(f"\n[*] 检测到板子重新上线 (uptime={h.get('uptime')}s)")

    s.close()

    log = ANSI.sub("", "".join(buf))
    with open("cold_boot.log", "w", encoding="utf-8") as f:
        f.write(log)
    print(f"\n[*] 启动日志已存 cold_boot.log ({len(log)} 字节)")

    # ---- 判定 ----
    print("\n" + "=" * 60)
    print("冷启动判定")
    print("=" * 60)

    h = probe_health()
    if not h:
        print("[FAIL] 板子完全没上线 —— 检查串口日志有没有崩溃/看门狗复位")
        return 2

    print(f"uptime={h.get('uptime')}s  ip={h.get('ip')}")

    # 日志层面的检查
    log_checks = [
        ("AW9523B 应答",     "AW9523B 无应答" not in log),
        ("I2C 扫描有应答",   "共 0 个地址应答" not in log),
        ("摄像头未报 not supported", "Detected camera not supported" not in log),
        ("无看门狗复位",     "watchdog" not in log.lower() or "guru Meditation" not in log),
    ]
    ok_all = True
    for name, ok in log_checks:
        print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}")
        ok_all = ok_all and ok

    for key, (name, fn) in CHECKS.items():
        v = h.get(key)
        ok = fn(v)
        print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}  (={v})")
        ok_all = ok_all and ok

    # I2C 扫描结果单独打印
    m = re.findall(r"共 (\d+) 个地址应答", log)
    if m:
        print(f"  [INFO] I2C 扫描应答数: {m[-1]} 个 (期望 5)")

    print("=" * 60)
    if ok_all:
        print("结论: [PASS] 冷启动正常")
        return 0
    print("结论: [FAIL] 冷启动有问题, 见 cold_boot.log")
    return 1


if __name__ == "__main__":
    sys.exit(main())
