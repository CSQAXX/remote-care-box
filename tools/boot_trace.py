"""
boot_trace.py —— 抓完整启动日志 (含开机内存快照)。

用法:
    python boot_trace.py [秒数] [跳过复位]

工作方式:
    先打开串口开始监听, 再用 esptool 复位板子 —— 这样第一行就是 boot 的第一行。
    (直接发 DTR/RTS 复位会让 USB Serial-JTAG 断开, 串口句柄失效)

为什么要这个:
    之前用 capture_boot.py 抓, 起点总是晚 20 秒, 而内存问题恰恰
    发生在启动早期, 抓不到就没法定位。
"""
import subprocess
import sys
import threading
import time

import serial

duration = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
skip_reset = len(sys.argv) > 2 and sys.argv[2] == "noreset"

PY = r"D:\Development Environment\ESP-IDF\python_env\Scripts\python.exe"


def do_reset():
    """在后台复位板子, 不等它结束。"""
    time.sleep(1.0)
    subprocess.run(
        [PY, "-m", "esptool", "--port", "COM3", "--chip", "esp32s3", "run"],
        capture_output=True,
    )


s = serial.Serial("COM3", 115200, timeout=0.2)

if not skip_reset:
    t = threading.Thread(target=do_reset, daemon=True)
    t.start()
else:
    s.reset_input_buffer()

chunks = []
end = time.time() + duration
while time.time() < end:
    try:
        data = s.read(s.in_waiting or 1)
    except serial.SerialException:
        break
    if data:
        chunks.append(data.decode("utf-8", errors="replace"))

s.close()
text = "".join(chunks)
out_path = "D:/Software/kanhu/xiaozhi-box3/diag/full_boot.txt"
with open(out_path, "w", encoding="utf-8") as f:
    f.write(text)

print("行数:", text.count("\n"))
print("--- 关键内存 / WiFi / 崩溃 行 ---")
for line in text.split("\n"):
    if any(k in line for k in (
        "内部RAM", "PSRAM:", "Wi-Fi STA", "从 NVS", "使用编译期",
        "TCB", "剩余=", "PANIC", "Guru", "abort", "wifi_cfg",
    )):
        print(line[:230])
