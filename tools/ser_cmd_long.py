"""
向板子发一条串口命令, 并持续读取 N 秒的完整输出。

为什么需要它:
    ser_cmd.py 只等 1.5 秒就读完, 但 batdiag 这种命令要跑好几秒
    (6 组组合 x 300ms 延时 + 5 次 ADC 采样), 1.5 秒只能抓到开头。
    诊断命令的价值全在完整输出上, 所以这里把等待时间拉长。

用法:
    python tools/ser_cmd_long.py batdiag 15
    python tools/ser_cmd_long.py bat 5
"""
import sys
import time

import serial

cmd = sys.argv[1] if len(sys.argv) > 1 else "bat"
wait = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0

s = serial.Serial("COM3", 115200, timeout=0.2)
time.sleep(0.5)
s.reset_input_buffer()
s.write((cmd + "\n").encode("utf-8"))

chunks = []
deadline = time.time() + wait
while time.time() < deadline:
    data = s.read_all()
    if data:
        chunks.append(data.decode("utf-8", errors="replace"))
    time.sleep(0.1)

s.close()

out = "".join(chunks)
# 去掉 ANSI 颜色码, 便于阅读
import re
out = re.sub(r"\x1b\[[0-9;]*m", "", out)
print(out)
