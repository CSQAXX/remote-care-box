# -*- coding: utf-8 -*-
"""M2 物理按键验收脚本

用途:
  1. 后台轮询 /event, 模拟网页接收看护事件
  2. 监听串口, 等待物理按键 (BOOT / K1 / K2) 产生事件
  3. 打印按键原始电平, 方便定位硬件问题

用法:
  python tools_m2_button_verify.py            # 等 60 秒, 期间按板子上的键
  python tools_m2_button_verify.py --inject   # 同时注入一条测试事件(自检)
"""
import argparse
import json
import re
import sys
import threading
import time
import urllib.request

import serial

HOST = "192.168.2.138"
PORT = 8080
SERIAL_PORT = "COM3"
BAUD = 115200


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def poll_events(stop_flag, out):
    """模拟网页: 轮询 /event 拿看护事件"""
    while not stop_flag[0]:
        try:
            r = urllib.request.urlopen(f"http://{HOST}:{PORT}/event", timeout=3)
            d = json.loads(r.read().decode("utf-8"))
            if d.get("type") == "button":
                out.append(d)
                print(f"  [网页收到事件] msg={d['msg']} seq={d.get('seq')}", flush=True)
        except Exception:
            pass
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--inject", action="store_true", help="额外注入一条测试事件")
    ap.add_argument("--host", default=HOST)
    args = ap.parse_args()

    events = []
    stop_flag = [False]
    th = threading.Thread(target=poll_events, args=(stop_flag, events), daemon=True)
    th.start()

    ser = serial.Serial(SERIAL_PORT, BAUD, timeout=0.2)
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:
        pass

    print(f"=== 监听中, 请在 {args.seconds} 秒内按板子上的按键 ===", flush=True)
    print("    BOOT(GPIO0) = 看护事件 | K1 = 音量- | K2 = 音量+", flush=True)

    time.sleep(1)
    if args.inject:
        ser.write("evtest 通路自检\n".encode("utf-8"))
        ser.flush()
        print("  [已注入测试事件]", flush=True)

    start = time.time()
    saw_event = False
    while time.time() - start < args.seconds:
        try:
            line = ser.readline()
            if not line:
                continue
            text = strip_ansi(line.decode("utf-8", "replace")).rstrip()
            if any(k in text for k in ("检测到", "生成事件", "音量", "按键", "电脑已确认")):
                print("  ", text[-100:], flush=True)
                if "生成事件" in text:
                    saw_event = True
        except Exception:
            pass

    stop_flag[0] = True
    ser.close()

    print("\n=== 结果 ===")
    print(f"串口看到物理按键事件: {'是' if saw_event else '否'}")
    print(f"网页收到事件数: {len(events)}")
    for e in events:
        print(f"  - {e['msg']}")
    return 0 if events else 1


if __name__ == "__main__":
    sys.exit(main())
