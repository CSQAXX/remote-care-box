"""决定性实验: 亮度跳动是"传感器真的变了"还是"硬件转换器/帧缓冲错位"?

对照:
  A) 当前模式 (YUV422 + 硬件转换器)      -> 抓亮度序列
  B) mode 1 (传感器直出 RGB565, 不用转换器) -> 抓亮度序列
  C) mode 3 (传感器直出 YUV422 真值)       -> 抓亮度序列

如果只有 A 在跳, B/C 稳定 -> 是硬件转换器的问题。
如果三个都跳   -> 是传感器/曝光的问题。
"""
import serial, time, re, numpy as np

PORT = "COM3"
DEV = r"D:\Software\\kanhu\\remote-care-box\\firmware"
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
    while time.time() - t0 < wait:
        try:
            s.readline()
        except Exception:
            pass


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


def capture(s, tag, n=8):
    ms = []
    for i in range(n):
        rawdump(s, f"cm_{tag}_{i}.bin")
        a = load(f"cm_{tag}_{i}.bin")
        ms.append(a.mean() if a is not None else -1)
        time.sleep(0.35)
    v = np.array([m for m in ms if m > 0])
    rng = (v.max() - v.min()) / max(v.min(), 1e-9) * 100
    print(f"  [{tag}] " + " ".join(f"{m:.0f}" for m in ms) +
          f"   幅度 {rng:.0f}%", flush=True)
    return rng


s = open_clean(3.0)
time.sleep(6.0)

for mode, name in ((0, "A: YUV422+硬件转换器"), (1, "B: 传感器直出RGB565"),
                    (3, "C: 传感器直出YUV422")):
    print(f"\n=== {name} ===", flush=True)
    cmd(s, f"mode {mode}", 8.0)
    time.sleep(3.0)
    capture(s, f"m{mode}")
    # 切回 mode 0 以便下次切换
    cmd(s, "mode 0", 8.0)
    time.sleep(2.0)
s.close()
