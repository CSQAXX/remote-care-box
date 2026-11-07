"""
连着 /stream 抓串口日志, 用来定位"拉流时帧率暴跌"的问题。

用法:
    python tools/stream_trace.py 12
"""
import re
import sys
import threading
import time
import urllib.request

import serial

wait = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0

ser = serial.Serial("COM3", 115200, timeout=0.2)
time.sleep(0.4)
ser.reset_input_buffer()

lines = []
stop = threading.Event()


def reader():
    while not stop.is_set():
        try:
            raw = ser.read_all().decode("utf-8", errors="replace")
        except Exception:
            break
        for ln in raw.splitlines():
            ln = re.sub(r"\x1b\[[0-9;]*m", "", ln).strip()
            if ln:
                lines.append(ln)


t = threading.Thread(target=reader, daemon=True)
t.start()

# 静置 3 秒, 记录基线帧率
time.sleep(3)
base = len([x for x in lines if "PROD:" in x])
print("--- 静置阶段 PROD 行数: %d ---" % base)

# 连流
n = 0
t0 = time.time()
try:
    r = urllib.request.urlopen("http://192.168.2.138:8080/stream", timeout=10)
    while time.time() - t0 < wait:
        b = r.read(4096)
        if not b:
            break
        n += len(b)
except Exception as e:
    print("ERR", e)

time.sleep(1.5)
stop.set()
ser.close()

print("--- 连流 %ds 收到 %d bytes ---" % (wait, n))
prod = [x for x in lines if "PROD:" in x]
print("--- PROD 行 (最后 12 条) ---")
for ln in prod[-12:]:
    print(ln)
print("--- 其它关键日志 ---")
for ln in lines:
    if any(k in ln for k in ("画面客户端", "推流客户端", "长连接", "拒绝", "503", "槽位")):
        print(ln)
