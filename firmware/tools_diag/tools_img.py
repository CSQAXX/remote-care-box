"""分析暗帧/亮帧的结构: 是整帧亮度差异, 还是行/列方向的渐变(说明曝光在帧内变化)。"""
import serial, time, re, numpy as np
from PIL import Image

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


def to_img(a):
    r = ((a >> 11) & 0x1F) * 255 // 31
    g = ((a >> 5) & 0x3F) * 255 // 63
    b = (a & 0x1F) * 255 // 31
    return np.dstack([r, g, b]).astype(np.uint8)


s = open_clean(3.0)
time.sleep(6.0)

frames = []
for i in range(6):
    rawdump(s, f"img_{i}.bin")
    frames.append(load(f"img_{i}.bin"))
    time.sleep(0.4)

for i, a in enumerate(frames):
    if a is None:
        continue
    Image.fromarray(to_img(a)).resize((W*2, H*2), Image.NEAREST).save(f"img_{i}.png")
    rows = a.mean(axis=1)
    # 每 30 行一档
    prof = " ".join(f"{rows[y]:5.0f}" for y in range(0, H, 30))
    print(f"帧{i}: mean={a.mean():8.0f}")
    print(f"      行剖面: {prof}")
    print(f"      首行 {rows[0]:.0f}  末行 {rows[-1]:.0f}  行间最大跳 {np.abs(np.diff(rows)).max():.0f}")

print("\n=== 暗帧 vs 亮帧 的比值 (逐像素) ===")
ms = [a.mean() if a is not None else 0 for a in frames]
if frames[0] is not None and frames[1] is not None:
    d = frames[1].astype(np.float64) / np.maximum(frames[0].astype(np.float64), 1)
    print(f"  帧1/帧0 比值: min={d.min():.3f} max={d.max():.3f} "
          f"中位={np.median(d):.3f} std={d.std():.4f}")
    print("  -> 比值接近常数 = 纯增益变化(AE); 比值随位置变 = 帧内/位置问题")
s.close()
