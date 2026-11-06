"""用新加的 cfg 串口命令确认开机默认配置是否真正生效。"""
import serial, time, re

PORT = "COM3"
s = serial.Serial(PORT, 115200, timeout=0.2)
try:
    s.dtr = False
    s.rts = False
except Exception:
    pass
t0 = time.time()
while time.time() - t0 < 3.0:
    try:
        s.readline()
    except Exception:
        pass

# 等固件跑起来
time.sleep(6.0)
s.write(b"cfg\n")
s.flush()

blob = bytearray()
t0 = time.time()
NEED = ("采集配置".encode(), b"PSRAM DMA", b"GC0308 PCLK")
while time.time() - t0 < 8.0:
    try:
        c = s.read(4096)
        if c:
            blob += c
            if all(n in blob for n in NEED):
                time.sleep(0.8)
                try:
                    blob += s.read(8192)
                except Exception:
                    pass
                break
    except Exception:
        pass
s.close()

txt = re.sub(r"\x1b\[[0-9;]*m", "", blob.decode("utf-8", "replace"))
print("=" * 66)
for line in txt.splitlines():
    line = line.strip()
    if line and ("采集配置" in line or "==" in line or "PSRAM" in line
                 or "PCLK" in line or "传感器模式" in line):
        print("  ", line[-90:])
print("=" * 66)

pda_ok = "PSRAM DMA 直写: 关" in txt
pclk_ok = "GC0308 PCLK 分频: 2" in txt
print(f"  [{'OK' if pda_ok else 'FAIL'}] PSRAM 直写已关闭")
print(f"  [{'OK' if pclk_ok else 'FAIL'}] PCLK 四分频 (div=2)")
print("  >>> 开机默认配置已确认生效" if (pda_ok and pclk_ok) else "  >>> 有项未生效, 需要复查")
