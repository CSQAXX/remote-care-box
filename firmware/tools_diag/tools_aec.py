"""读 AEC 真正的判决寄存器 (0xd0-0xd3 / 0x54-0x59), 确认是不是 AE 在来回跳。"""
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
time.sleep(6.0)

print("=== 连续 8 轮读 0x50-0x5f (AGC/AWB/AEC 判决区) ===", flush=True)
rows = []
for i in range(8):
    r = parse(cmd(s, "regs 0x50 0x5f", 5.0))
    rows.append(r)
    time.sleep(0.4)
for reg in sorted(rows[0].keys()):
    seq = [rows[i].get(reg) for i in range(len(rows))]
    changed = len({v for v in seq if v is not None}) > 1
    print(f"  0x{reg:02x}: " + " ".join(f"{v:02X}" for v in seq) +
          ("   <== 在变" if changed else ""))

print("\n=== 连续 8 轮读 0xd0-0xdf (AEC 判决寄存器) ===", flush=True)
rows2 = []
for i in range(8):
    r = parse(cmd(s, "regs 0xd0 0xdf", 5.0))
    rows2.append(r)
    time.sleep(0.4)
for reg in sorted(rows2[0].keys()):
    seq = [rows2[i].get(reg) for i in range(len(rows2))]
    changed = len({v for v in seq if v is not None}) > 1
    print(f"  0x{reg:02x}: " + " ".join(f"{v:02X}" for v in seq) +
          ("   <== 在变" if changed else ""))
s.close()
