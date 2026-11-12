"""
ser_capture.py —— 抓一段时间的串口输出, 并可顺带发命令。

用法:
    python ser_capture.py <秒数> [命令...]
    python ser_capture.py 20 scan
    python ser_capture.py 30 wifiset "校园网" 密码123

不发命令时就是纯监听。
"""
import serial
import sys
import time

duration = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
commands = sys.argv[2:]

s = serial.Serial("COM3", 115200, timeout=0.3)
time.sleep(0.5)
s.reset_input_buffer()

if commands:
    # 逐条发, 每条之间留间隔, 让板子有时间回
    for cmd in commands:
        s.write((cmd + "\n").encode("utf-8"))
        s.flush()
        time.sleep(2.0)

end = time.time() + duration
chunks = []
while time.time() < end:
    data = s.read(s.in_waiting or 1)
    if data:
        chunks.append(data.decode("utf-8", errors="replace"))

s.close()
text = "".join(chunks)
print(text)
with open("D:/Software/kanhu/xiaozhi-box3/diag/ser_out.txt", "w", encoding="utf-8") as f:
    f.write(text)
