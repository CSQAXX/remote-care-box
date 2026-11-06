"""检验: 暗帧和亮帧是不是"同一画面的不同缩放"(传感器窗口/采样模式在变)?

判据:
  - 把亮帧缩放到暗帧尺寸, 和暗帧比较 -> 如果高度相似, 就是缩放差异
  - 把暗帧缩放到亮帧尺寸, 和亮帧比较 -> 同上
  - 如果都不相似, 就真的是不同内容
另外: 检查驱动是否启用了 GC0308 的 subsample 模式 (会动态改窗口)。
"""
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


def cmd(s, c, wait=1.0):
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


def to_gray(a):
    r = ((a >> 11) & 0x1F) * 255 // 31
    g = ((a >> 5) & 0x3F) * 255 // 63
    b = (a & 0x1F) * 255 // 31
    return (0.299*r + 0.587*g + 0.114*b).astype(np.float64)


s = open_clean(3.0)
time.sleep(9.0)

# 抓一堆帧, 分成暗组和亮组
frames = []
for i in range(14):
    rawdump(s, f"zm_{i}.bin")
    a = load(f"zm_{i}.bin")
    if a is not None:
        frames.append((a, a.mean()))
    time.sleep(0.4)

darks = [f for f in frames if f[1] < 18000]
brights = [f for f in frames if f[1] >= 18000]
print(f"抓到 {len(frames)} 帧: 暗 {len(darks)}, 亮 {len(brights)}")
if not darks or not brights:
    print("样本不够"); s.close(); raise SystemExit

d = darks[0][0]
b = brights[0][0]
gd, gb = to_gray(d), to_gray(b)

def cmp(name, A, B):
    """A 是参考, B 是被缩放过来的; 用归一化互相关衡量结构相似度"""
    a = A - A.mean()
    b = B - B.mean()
    denom = np.sqrt((a*a).sum() * (b*b).sum())
    ncc = float((a*b).sum()/denom) if denom > 0 else 0
    print(f"  {name}: NCC={ncc:.4f}  MAE={np.abs(A-B).mean():.1f}")
    return ncc

print("\n=== 直接比较 (暗 vs 亮) ===")
cmp("原尺寸", gd, gb)

print("\n=== 缩放后比较 ===")
im_b = Image.fromarray(gb.astype(np.uint8))
im_d = Image.fromarray(gd.astype(np.uint8))
# 把亮帧缩小到 160x120, 暗帧也缩小到 160x120, 比较
for scale in (0.9, 0.75, 0.5, 0.25):
    w2, h2 = int(W*scale), int(H*scale)
    bs = np.asarray(im_b.resize((w2, h2), Image.BILINEAR), dtype=np.float64)
    ds = np.asarray(im_d.resize((w2, h2), Image.BILINEAR), dtype=np.float64)
    # 归一化到同均值再比
    bs_n = bs / max(bs.mean(), 1e-6)
    ds_n = ds / max(ds.mean(), 1e-6)
    a = ds_n - ds_n.mean(); bb = bs_n - bs_n.mean()
    denom = np.sqrt((a*a).sum()*(bb*bb).sum())
    ncc = float((a*bb).sum()/denom) if denom > 0 else 0
    print(f"  scale={scale}: 归一化后 NCC={ncc:.4f}")

print("\n=== 反向: 把暗帧放大到亮帧 ===")
ds_up = np.asarray(im_d.resize((W, H), Image.BILINEAR), dtype=np.float64)
bs_dn = np.asarray(im_b.resize((W//2, H//2), Image.BILINEAR), dtype=np.float64)
print(f"  暗帧mean={gd.mean():.0f}  亮帧mean={gb.mean():.0f}")
s.close()
