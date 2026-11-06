"""抓 AE/AGC/AWB 关键寄存器, 判定亮度跳动是不是 AE 行为。

关键寄存器:
  0x03/0x04  EXPOSURE_HIGH/LOW   曝光时间
  0x22       AAAA_EN              bit0=AEC bit1=AWB bit2=AGC
  0x50       GLOBAL_GAIN          全局增益
  0x5a-0x5c  AWB_R/G/B_GAIN
  0xd0-0xd3  AEC_MODE1/2/3 + AEC_TARGET_Y (需要先选页, 这里只读 page0 的)
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


s = open_clean(3.0)
time.sleep(6.0)

rows = []
for i in range(8):
    rawdump(s, f"ae2_{i}.bin")
    r = parse(cmd(s, "regs 0x00 0x5f", 6.0))
    a = load(f"ae2_{i}.bin")
    rows.append((r, a.mean() if a is not None else -1))
    print(f"  轮{i}: mean={rows[-1][1]:8.0f}  读到 {len(r)} 个寄存器", flush=True)
    time.sleep(0.5)

print("\n=== 亮度 ===")
print("  " + "  ".join(f"{m:.0f}" for _, m in rows))

print("\n=== 关键寄存器序列 ===")
for reg, name in ((0x03, "EXPOSURE_HIGH"), (0x04, "EXPOSURE_LOW"),
                  (0x22, "AAAA_EN(AEC/AWB/AGC)"), (0x50, "GLOBAL_GAIN"),
                  (0x5a, "AWB_R"), (0x5b, "AWB_G"), (0x5c, "AWB_B")):
    seq = [r.get(reg) for r, _ in rows]
    if any(v is not None for v in seq):
        changed = len({v for v in seq if v is not None}) > 1
        print(f"  0x{reg:02x} {name:20s}: " +
              " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in seq) +
              ("   <== 在变" if changed else ""))

print("\n=== 结论 ===")
changed = [reg for reg in (0x03, 0x04, 0x22, 0x50, 0x5a, 0x5b, 0x5c)
           if len({r.get(reg) for r, _ in rows if r.get(reg) is not None}) > 1]
ms = [m for _, m in rows]
print(f"  亮度范围 {min(ms):.0f} ~ {max(ms):.0f} (幅度 {100*(max(ms)-min(ms))/min(ms):.0f}%)")
if changed:
    print(f"  在变的寄存器: {[hex(c) for c in changed]}")
    print("  -> 是传感器自动算法(AE/AGC/AWB)在跳")
else:
    print("  所有关键寄存器都不变 -> 不是自动算法, 是数据链路/时序问题")
s.close()
