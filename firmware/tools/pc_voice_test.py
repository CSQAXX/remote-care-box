# -*- coding: utf-8 -*-
"""喊一句 + 抓串口日志。

用法: python tools/pc_voice_test.py "小电脑，打开微信"
- 先开串口(会复位板子) -> 等开机自检完 -> 用电脑喇叭念这句 -> 继续抓 45 秒
- 日志存 output/pc_voice_test.txt
"""
import os
import sys
import threading
import time

import serial

PORT = "COM3"
BAUD = 115200
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "output", "pc_voice_test.txt")


def speak(text):
    import win32com.client
    v = win32com.client.Dispatch("SAPI.SpVoice")
    # 优先挑中文音色
    try:
        for tok in v.GetVoices():
            if "Chinese" in tok.GetDescription() or "Huihui" in tok.GetDescription():
                v.Voice = tok
                break
    except Exception:
        pass
    v.Rate = -1
    v.Volume = 100
    v.Speak(text)


def main():
    phrase = sys.argv[1] if len(sys.argv) > 1 else "小电脑，打开微信"
    tail = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    ser = serial.Serial(PORT, BAUD, timeout=0.2)
    buf = []

    # 硬复位一次, 保证每次测试都是干净开机 (不清 NVS, 只复位芯片)
    ser.setDTR(False)
    ser.setRTS(True)
    time.sleep(0.15)
    ser.setRTS(False)
    time.sleep(0.1)

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            d = ser.readline()
            if not d:
                continue
            line = d.decode("utf-8", "replace").rstrip()
            if line:
                buf.append("%8.2f  %s" % (time.time() - t0, line))
                print(buf[-1])

    t0 = time.time()
    print("等板子开机...")
    pump(18)
    print("=== 开始念: %s ===" % phrase)
    for _ in range(3):
        threading.Thread(target=speak, args=(phrase,), daemon=True).start()
        pump(10)
    pump(max(tail - 30, 10))
    ser.close()
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    print("日志已存 %s (%d 行)" % (OUT, len(buf)))


if __name__ == "__main__":
    main()
