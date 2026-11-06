import serial, time, re, sys, numpy as np

port = "COM3"
DEV = "D:\\Software\\kanhu\\remote-care-box\\firmware"

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

W, H = 320, 240
def load(p):
    d = open(DEV + "\\" + p, "rb").read()
    if len(d) != W*H*2: return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

def scene_quality(s, tag):
    """真实场景: 抓两帧, 归一化曝光后比对"""
    time.sleep(2.0)
    rawdump(s, f"q_{tag}_a.bin")
    time.sleep(0.4)
    rawdump(s, f"q_{tag}_b.bin")
    a, b = load(f"q_{tag}_a.bin"), load(f"q_{tag}_b.bin")
    if a is None or b is None:
        return None
    na = a.astype(np.float64) / (a.mean() or 1)
    nb = b.astype(np.float64) / (b.mean() or 1)
    d = np.abs(na - nb)
    return (100*(d>4).sum()/(W*H), 100*(d>100).sum()/(W*H))

s = open_clean(3.0)
for pclk in (1, 2, 3):
    cmd(s, f"pclk {pclk}", 3.0)
    res = scene_quality(s, f"pclk{pclk}")
    # 帧率
    time.sleep(6)
    fps = None
    for l in cmd(s, "q", 1.0):
        pass
    for l in cmd(s, "stat", 1.0):
        if "cam_frames" in l:
            pass
    # 直接读 PROD 行
    s.write(b"\n")
    time.sleep(4)
    prod = None
    t0 = time.time()
    while time.time() - t0 < 1.5:
        try:
            line = s.readline()
            if line and b"PROD" in line:
                prod = line.decode("utf-8","replace")
                break
        except Exception:
            pass
    print(f"pclk {pclk}: 场景归一化差异>4 = {res[0]:.2f}%  >100 = {res[1]:.2f}%   {prod[-60:] if prod else ''}", flush=True)
s.close()
