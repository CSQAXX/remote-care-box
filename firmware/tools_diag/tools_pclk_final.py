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
    while time.time() - t0 < wait:
        try:
            s.readline()
        except Exception:
            pass

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

def bitflip(a, b):
    x = a ^ b
    return 100 * sum(((x >> bit) & 1).sum() for bit in range(16)) / 16 / (W*H)

def flatness(a):
    """彩条每列应完全平坦: 统计列内与列众数不同的像素占比"""
    bad = 0
    for x in range(W):
        col = a[:, x]
        v, c = np.unique(col, return_counts=True)
        bad += (col != v[np.argmax(c)]).sum()
    return 100 * bad / (W*H)

s = open_clean(3.0)
for pclk in (0, 1, 2, 3):
    cmd(s, f"pclk {pclk}", 3.0)
    cmd(s, "cbar 1", 1.5)
    time.sleep(3.0)
    fs = []
    for i in range(4):
        rawdump(s, f"c{pclk}_{i}.bin")
        fs.append(load(f"c{pclk}_{i}.bin"))
        time.sleep(0.4)
    cmd(s, "cbar 0", 1.0)
    flips = [bitflip(fs[i], fs[i+1]) for i in range(3)]
    flats = [flatness(f) for f in fs]
    print(f"pclk {pclk}: bit翻转率 = {['%.2f%%'%v for v in flips]}   "
          f"列内平坦度 = {['%.2f%%'%v for v in flats]}", flush=True)

s.close()
