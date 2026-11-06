"""把 0xd4 (疑似 AEC 当前曝光) 和画面亮度一一对应, 确认因果关系。

同时对比 0x03/0x04 (手动曝光) 和 0x0a-0x11 (曝光窗口) 的实际值。
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

print("=== 亮度 vs 0xd4 (AEC 当前曝光) 一一对应 ===", flush=True)
print(f"{'轮':>3s} {'亮度':>8s} {'0xd4':>5s} {'0xd0':>5s} {'0xd1':>5s} {'0xd2':>5s} "
      f"{'0xd3':>5s} {'0x03':>5s} {'0x04':>5s} {'0x22':>5s}")
rows = []
for i in range(12):
    rawdump(s, f"cd_{i}.bin")
    a = load(f"cd_{i}.bin")
    m = a.mean() if a is not None else -1
    r = parse(cmd(s, "regs 0xd0 0xd4", 4.0))
    r2 = parse(cmd(s, "regs 0x03 0x04", 3.0))
    r3 = parse(cmd(s, "regs 0x22 0x22", 3.0))
    g = lambda k, d: (r.get(k) or r2.get(k) or r3.get(k) or d)
    print(f"{i:3d} {m:8.0f} {g(0xd4,0):5d} {g(0xd0,0):5d} {g(0xd1,0):5d} "
          f"{g(0xd2,0):5d} {g(0xd3,0):5d} {g(0x03,0):5d} {g(0x04,0):5d} {g(0x22,0):5d}",
          flush=True)
    rows.append((m, g(0xd4, 0)))
    time.sleep(0.3)

print("\n=== 相关性 ===")
br = np.array([r[0] for r in rows])
d4 = np.array([r[1] for r in rows])
if d4.std() > 0 and br.std() > 0:
    print(f"  Pearson(亮度, 0xd4) = {np.corrcoef(br, d4)[0,1]:.4f}")
    for v in sorted(set(d4.tolist())):
        sel = d4 == v
        print(f"    0xd4={v}: 平均亮度 {br[sel].mean():8.0f}  (n={sel.sum()})")
s.close()
