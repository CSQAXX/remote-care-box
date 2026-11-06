# 诊断脚本: 复位板子后, 每隔一段时间读一次 AW9523B 寄存器,
# 找出 I2C 总线从"不通"变成"通"的时间点。
#
# 用法: python tools/i2c_probe.py [轮询秒数] [间隔秒数]
import serial, time, sys

total = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
gap = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0

s = serial.Serial("COM3", 115200, timeout=0.2)
s.setDTR(False)
s.setRTS(False)
time.sleep(0.3)
s.setRTS(True)
time.sleep(0.2)
s.setRTS(False)
s.reset_input_buffer()

start = time.time()
t = 0.0
while t < total:
    s.reset_input_buffer()
    s.write(b"awregs\n")
    time.sleep(0.6)
    out = s.read_all().decode("utf-8", errors="replace")
    ok = "ID=0x23" in out
    print("t=%5.1fs  AW9523B=%s" % (t, "OK" if ok else "无应答"), flush=True)
    t += gap

s.close()
