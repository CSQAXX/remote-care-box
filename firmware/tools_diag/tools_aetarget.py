"""最后一轮硬件侧尝试: AEC 目标亮度 (0xd3) x PCLK 分频。

如果 AE 是在两个台阶之间来回跳, 换目标值可能让它停在某一级。
"""
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


def cmd(s, c, wait=0.9):
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


def watch(s, secs=20):
    t0 = time.time()
    prev = None
    ch = 0
    vals = set()
    while time.time() - t0 < secs:
        v = parse(cmd(s, "regs 0 0 0d4 0d4", 0.8)).get(0xd4)
        if v is not None:
            vals.add(v)
            if prev is not None and v != prev:
                ch += 1
            prev = v
        time.sleep(0.25)
    return ch, sorted(vals)


s = open_clean(3.0)
time.sleep(9.0)

print("=== AEC 目标亮度 0xd3 扫描 ===", flush=True)
for tgt in (0x20, 0x30, 0x40, 0x48, 0x60, 0x80, 0xA0, 0xC0):
    cmd(s, f"wr d3 {tgt:02X}", 0.6)
    time.sleep(1.0)
    ch, vals = watch(s, 20)
    st = "*** 稳定! ***" if ch == 0 else f"变{ch}次"
    print(f"  d3={tgt:02X}  0xd4={[format(v,'02X') for v in vals]}  {st}", flush=True)

cmd(s, "wr d3 48", 0.6)

print("\n=== PCLK 分频对震荡的影响 (d3 恢复 0x48) ===", flush=True)
for div in (1, 2, 3):
    cmd(s, f"pclk {div}", 1.5)
    time.sleep(2.0)
    ch, vals = watch(s, 22)
    st = "*** 稳定! ***" if ch == 0 else f"变{ch}次"
    print(f"  pclk={div}  0xd4={[format(v,'02X') for v in vals]}  {st}", flush=True)
cmd(s, "pclk 2", 1.5)
s.close()
