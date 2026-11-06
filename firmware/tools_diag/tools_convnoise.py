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

def flatness_2d(a):
    """3x3 中值滤波残差 = 空间噪声能量 (越大越花)"""
    p = np.pad(a.astype(np.float64), 1, mode='edge')
    stack = np.stack([p[0:-2,0:-2], p[0:-2,1:-1], p[0:-2,2:],
                      p[1:-1,0:-2], p[1:-1,1:-1], p[1:-1,2:],
                      p[2:,0:-2],   p[2:,1:-1],   p[2:,2:]], axis=0)
    med = np.median(stack, axis=0)
    r = np.abs(a.astype(np.float64) - med)
    return r.mean(), 100*(r>3).sum()/a.size

s = open_clean(3.0)
print("=== 对比不同传感器模式的空间噪声 (pda0 + pclk2) ===", flush=True)
for mode, label in ((0, "YUV422+硬件转换器"), (1, "传感器直出RGB565")):
    cmd(s, f"mode {mode}", 8.0)
    time.sleep(3.0)
    rawdump(s, f"m{mode}.bin")
    time.sleep(0.4)
    a = np.frombuffer(open(DEV+f"\\m{mode}.bin","rb").read(), dtype=">u2").reshape(H,W).astype(np.int32)
    r5 = (a>>11)&0x1F; g6=(a>>5)&0x3F; b5=a&0x1F
    print(f"  mode {mode} ({label}):", flush=True)
    for nm, ch in (("R5",r5),("G6",g6),("B5",b5)):
        m, pct = flatness_2d(ch)
        print(f"      {nm}: 3x3中值残差 均值={m:.3f}  >3占比={pct:.2f}%")
    cmd(s, "mode 0", 8.0)
s.close()
print("done")
