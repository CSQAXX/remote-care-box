import serial, time, re

port = "COM3"
s = serial.Serial(port, 115200, timeout=0.3)
try:
    s.dtr = False
    s.rts = False
except Exception:
    pass
t0 = time.time()
while time.time() - t0 < 2.5:
    try:
        s.readline()
    except Exception:
        pass
s.dtr = False
s.rts = True
time.sleep(0.2)
s.rts = False
time.sleep(0.3)
t0 = time.time()
out = []
while time.time() - t0 < 20:
    try:
        line = s.readline()
        if line:
            out.append(line.decode("utf-8", "replace"))
    except Exception:
        pass
s.close()
clean = [re.sub(r"\x1b\[[0-9;]*m", "", l).rstrip() for l in out]
print(f"共 {len(clean)} 行, 全部输出:\n")
for l in clean:
    if "kanhu" in l or "lcd" in l or "cam" in l or "gc0308" in l or "camera" in l:
        print(l)
