import serial, time, sys

# 复位板子并抓完整开机日志
s = serial.Serial("COM3", 115200, timeout=0.2)
s.reset_input_buffer()

# 触发复位 (DTR/RTS 序列)
s.setDTR(False)
s.setRTS(True)
time.sleep(0.1)
s.setRTS(False)
time.sleep(0.5)
s.reset_input_buffer()

buf = b""
end = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 30)
while time.time() < end:
    try:
        d = s.read(4096)
        if d:
            buf += d
    except Exception:
        break
s.close()

out = sys.argv[2] if len(sys.argv) > 2 else "boot_full.log"
with open(out, "wb") as f:
    f.write(buf)
print("saved", out, len(buf), "bytes")
