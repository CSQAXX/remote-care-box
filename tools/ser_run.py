import serial, sys, time

# 发一条串口命令, 把输出写到文件 (避开 Windows 控制台 GBK 编码问题)。
# 用法: python tools/ser_run.py <等待秒数> <输出文件> <命令...>
wait = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
outfile = sys.argv[2] if len(sys.argv) > 2 else "ser_out.txt"
cmd = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "stat"

s = serial.Serial("COM3", 115200, timeout=0.2)
time.sleep(0.5)
s.reset_input_buffer()
s.write((cmd + "\n").encode("utf-8"))

buf = b""
end = time.time() + wait
while time.time() < end:
    try:
        d = s.read(4096)
        if d:
            buf += d
    except Exception:
        break
s.close()

with open(outfile, "wb") as f:
    f.write(buf)
print("saved", outfile, len(buf), "bytes")
