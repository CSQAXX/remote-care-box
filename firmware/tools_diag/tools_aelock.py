"""验证 aelock 能否消除画面亮度跳动。"""
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


def cmd(s, c, wait=2.0, show=False):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                t = line.decode("utf-8", "replace").rstrip()
                out.append(t)
                if show:
                    print("   ", t[-90:])
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


def capture(s, tag, n=8, gap=0.4):
    ms = []
    for i in range(n):
        rawdump(s, f"al_{tag}_{i}.bin")
        a = load(f"al_{tag}_{i}.bin")
        ms.append(a.mean() if a is not None else -1)
        time.sleep(gap)
    return ms


def report(tag, ms):
    v = np.array(ms)
    rng = (v.max() - v.min()) / max(v.min(), 1e-9) * 100
    print(f"  [{tag}] mean序列: " + " ".join(f"{m:.0f}" for m in ms))
    print(f"        幅度 {rng:.1f}%  {'OK (稳定)' if rng < 15 else 'FAIL (还在跳)'}")
    return rng


s = open_clean(3.0)
time.sleep(6.0)

print("=== A) AE 未锁定 (基线) ===", flush=True)
ms0 = capture(s, "auto")
report("AE 自动", ms0)

print("\n=== B) 锁定 AE (aelock 1) ===", flush=True)
cmd(s, "aelock 1", 3.0, show=True)
time.sleep(2.0)
ms1 = capture(s, "lock")
report("AE 锁定", ms1)

print("\n=== C) 调一下曝光时间看是否还能更稳 ===", flush=True)
for t in (300, 480, 700):
    cmd(s, f"expo {t} 20", 1.5, show=False)
    time.sleep(1.5)
    m = capture(s, f"t{t}", 5)
    report(f"expo {t}", m)

print("\n=== D) 解锁 AE 确认是 AE 干的 ===", flush=True)
cmd(s, "aelock 0", 2.0)
time.sleep(2.0)
ms3 = capture(s, "unlock")
report("AE 解锁", ms3)
s.close()
