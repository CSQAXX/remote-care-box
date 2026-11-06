"""找出真正能"钉死"曝光的寄存器。

0xd4 是只读输出(写不进去)。真正的手动曝光通路应该是:
  - 0x03/0x04: 曝光时间 (AEC 关时生效)
  - 0x50: 全局增益
  - 0x56/0x57/0x58/0x59: 曝光/增益相关 (AWB 那组的邻居)
  - 0x24/0x25: 可能是曝光上限
  - 0x94: AE 相关
  - 0x56/0x57: 手动曝光上限/下限 (很多 CMOS 传感器用这个)

关键实验: 关 AEC 之后, 把 0x03/0x04 写成【和当前 AE 判决不同的值】,
如果画面亮度跟着变, 说明手动曝光通路生效了; 如果不变, 说明 AEC 还在控制。

然后: 关 AEC + 写 0x03/0x04 -> 是否彻底稳定。
"""
import serial, time, re, numpy as np

PORT = "COM3"
DEV = r"D:\Software\kanhu\remote-care-box\firmware"
W, H = 320, 240


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


def cmd(s, c, wait=1.2):
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


def rawdump(s, path):
    s.write(b"rawdump\n")
    s.flush()
    buf = bytearray()
    while not buf.endswith(b"RAWDUMP "):
        c = s.read(1)
        if not c:
            return None
        buf += c
    hdr = bytes(buf)
    while not hdr.endswith(b"\n"):
        c = s.read(1)
        if not c:
            break
        hdr += c
    m = re.search(rb"RAWDUMP (\d+) (\d+)", hdr)
    if not m:
        return None
    n = int(m.group(2))
    data = bytearray()
    while len(data) < n:
        chunk = s.read(min(65536, n - len(data)))
        if not chunk:
            break
        data += chunk
    open(DEV + "\\" + path, "wb").write(data)
    return len(data)


def load(p):
    d = open(DEV + "\\" + p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)


def parse(lines):
    out = {}
    for l in lines:
        m = re.search(r"REG 0x([0-9a-fA-F]{2}) = 0x([0-9a-fA-F]{2})", l)
        if m:
            out[int(m.group(1), 16)] = int(m.group(2), 16)
    return out


def brightness(s, n=5):
    vals = []
    for i in range(n):
        rawdump(s, f"bt_{i}.bin")
        a = load(f"bt_{i}.bin")
        vals.append(a.mean() if a is not None else -1)
        time.sleep(0.3)
    return vals


s = open_clean(3.0)
time.sleep(10.0)

print("=== 1) 现在的实际寄存器值 ===", flush=True)
r = parse(cmd(s, "regs 0 255", 25.0))
for reg in (0x03, 0x04, 0x0a, 0x0b, 0x0c, 0x10, 0x11, 0x12, 0x22, 0x24, 0x25,
            0x50, 0x56, 0x57, 0x58, 0x59, 0x94, 0xd0, 0xd1, 0xd2, 0xd3, 0xd4):
    if reg in r:
        print(f"    0x{reg:02x} = 0x{r[reg]:02X} ({r[reg]})")

print("\n=== 2) 手动写 0x03/0x04 (曝光时间), 看亮度跟不跟 ===", flush=True)
for t in (0x1E0, 0x0B0, 0x3C0):
    cmd(s, f"wr 03 {(t >> 4) & 0xFF:02X}", 0.8)
    cmd(s, f"wr 04 {(t << 4) & 0xF0:02X}", 0.8)
    time.sleep(1.0)
    b = brightness(s, 4)
    print(f"    expo 0x{t:03X} -> 0x03={(t>>4)&0xFF:02X} 0x04={(t<<4)&0xF0:02X}  "
          f"亮度 {' '.join(f'{v:.0f}' for v in b)}", flush=True)

print("\n=== 3) 全关 AEC/AGC/AWB + 固定曝光 ===", flush=True)
cmd(s, "wr 22 50", 0.8)
cmd(s, "wr 03 1E", 0.8)
cmd(s, "wr 04 E0", 0.8)
cmd(s, "wr 50 14", 0.8)
time.sleep(2.0)
b = brightness(s, 10)
arr = np.array(b)
print(f"    亮度 {' '.join(f'{v:.0f}' for v in b)}  幅度 {(arr.max()-arr.min())/arr.min()*100:.0f}%",
      flush=True)
r2 = parse(cmd(s, "regs 0 255", 25.0))
print(f"    0x22={r2.get(0x22,0):02X} 0x03={r2.get(0x03,0):02X} 0x04={r2.get(0x04,0):02X} "
      f"0x50={r2.get(0x50,0):02X} 0xd4={r2.get(0xd4,0):02X}")
s.close()
