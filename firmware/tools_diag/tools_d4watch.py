"""极简长时观测: 只在 0xd4 变化时打印, 60 秒。"""
import serial, time, re

PORT = "COM3"


def open_clean(drain_s=3.0):
    s = serial.Serial(PORT, 115200, timeout=0.2)
    try:
        s.dtr = False
        s.rts = False
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < drain_s:
        try:
            s.readline()
        except Exception:
            pass
    return s


def cmd(s, c, wait=2.0):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                out.append(line.decode("utf-8", "replace"))
        except Exception:
            pass
    return out


def parse(lines):
    out = {}
    for l in lines:
        m = re.search(r"REG 0x([0-9a-fA-F]{2}) = 0x([0-9a-fA-F]{2})", l)
        if m:
            out[int(m.group(1), 16)] = int(m.group(2), 16)
    return out


s = open_clean(3.0)
time.sleep(7.0)
print("=== 开始观测 (当前状态, 每 0.5s 读一次 0xd4, 60 秒) ===", flush=True)
t0 = time.time()
prev = None
changes = 0
while time.time() - t0 < 60:
    r = parse(cmd(s, "regs 0xd4 0xd4", 1.2))
    d4 = r.get(0xd4)
    t = time.time() - t0
    if prev is not None and d4 != prev:
        changes += 1
        print(f"  t={t:6.1f}s  0xd4: {prev:02X} -> {d4:02X}   (第 {changes} 次变化)", flush=True)
    prev = d4
    time.sleep(0.3)
print(f"=== 60 秒内共变化 {changes} 次 ===", flush=True)
s.close()
