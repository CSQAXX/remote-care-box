"""验证: 写 0x03/0x04/0x50 (手动曝光) 是不是把传感器搞回震荡状态。

对照:
  A) aelock 1 (只写 0x22=0x52)                -> 期望稳定
  B) aelock 1 + expo 480 20 (写 0x03/04/50)   -> 看是否又开始震荡
  C) aelock 1 (再关一次, 不动曝光)           -> 看能否恢复稳定
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


def series(s, tag, n=8):
    br, d4, r22, r03, r04, r50 = [], [], [], [], [], []
    for i in range(n):
        rawdump(s, f"ord_{tag}_{i}.bin")
        a = load(f"ord_{tag}_{i}.bin")
        br.append(a.mean() if a is not None else -1)
        r = parse(cmd(s, "regs 0xd4 0xd4", 3.0))
        d4.append(r.get(0xd4))
        r22.append(parse(cmd(s, "regs 0x22 0x22", 3.0)).get(0x22))
        r03.append(parse(cmd(s, "regs 0x03 0x03", 3.0)).get(0x03))
        r04.append(parse(cmd(s, "regs 0x04 0x04", 3.0)).get(0x04))
        r50.append(parse(cmd(s, "regs 0x50 0x50", 3.0)).get(0x50))
        time.sleep(0.25)
    br = np.array(br)
    rng = (br.max()-br.min())/br.min()*100
    print(f"  [{tag}] 亮度 " + " ".join(f"{v:.0f}" for v in br) + f"  幅度 {rng:.0f}%")
    print(f"         0xd4 " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in d4)
          + f"  0x22 " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in r22))
    print(f"         0x03 " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in r03)
          + f"  0x04 " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in r04)
          + f"  0x50 " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in r50),
          flush=True)
    return rng, len(set(d4))


s = open_clean(3.0)
time.sleep(7.0)

print("=== A) aelock 1 (只写 0x22) ===", flush=True)
cmd(s, "aelock 1", 3.0)
time.sleep(2.0)
series(s, "A")

print("\n=== B) expo 480 20 (再写 0x03/0x04/0x50) ===", flush=True)
cmd(s, "expo 480 20", 3.0)
time.sleep(2.0)
series(s, "B")

print("\n=== C) 再 aelock 1 (不动曝光) ===", flush=True)
cmd(s, "aelock 1", 3.0)
time.sleep(2.0)
series(s, "C")
s.close()
