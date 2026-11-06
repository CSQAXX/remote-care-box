import serial, time, re, numpy as np

port = "COM3"
DEV = "D:\\Software\\kanhu\\remote-care-box\\firmware"
W, H = 320, 240

def open_clean(drain_s=3.0):
    s = serial.Serial(port, 115200, timeout=0.2)
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
                out.append(line.decode("utf-8", "replace").rstrip())
        except Exception:
            pass
    return out

def rawdump(s, path):
    s.write(b"rawdump\n")
    s.flush()
    buf = bytearray()
    needle = b"RAWDUMP "
    while not buf.endswith(needle):
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
    if len(d) != W*H*2: return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

s = open_clean(3.0)
cmd(s, "pclk 2", 3.0)
print("=== 真实场景: 连抓 6 帧, 全部两两对比 (归一化曝光后) ===", flush=True)
time.sleep(2.0)
frames = []
for i in range(6):
    rawdump(s, f"v{i}.bin")
    frames.append(load(f"v{i}.bin"))
    time.sleep(0.35)

worst = 0.0
for i in range(len(frames)-1):
    a, b = frames[i].astype(np.float64), frames[i+1].astype(np.float64)
    na = a/(a.mean() or 1); nb = b/(b.mean() or 1)
    d = np.abs(na-nb)
    pct4 = 100*(d>4).sum()/(W*H)
    worst = max(worst, pct4)
    print(f"  v{i} vs v{i+1}: 差异>4 {pct4:5.2f}%  >100 {100*(d>100).sum()/(W*H):5.2f}%  "
          f"max|Δ| {d.max():.1f}")

print(f"\n  >>> 全部 5 组配对中最差: {worst:.2f}%")
print("\n=== 固件质量指标 ===", flush=True)
for l in cmd(s, "q", 3.0):
    if "QUAL" in l or "hi_energy" in l:
        print("   ", l[-115:])
s.close()
