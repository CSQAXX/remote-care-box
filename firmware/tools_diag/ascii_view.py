import numpy as np, glob, io
from PIL import Image

def ascii_art(gray, w=64):
    h = max(1, int(gray.shape[0] * w / gray.shape[1] * 0.5))
    g = np.asarray(Image.fromarray(gray.astype(np.uint8)).resize((w, h), Image.BOX), dtype=float)
    chars = " .:-=+*#%@"
    lo, hi = g.min(), g.max()
    rng = max(1e-6, hi - lo)
    out = []
    for r in g:
        out.append("".join(chars[min(9, int((v - lo) / rng * 9.999))] for v in r))
    return "\n".join(out)

# ---- 1) RAW frame from camera ----
fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
d = open(fs[0], 'rb').read()
i = d.find(b'\r\n\r\n')
f = np.frombuffer(d[i + 4:i + 4 + 153600], dtype=np.uint8).reshape(240, 320, 2)
hi = f[:, :, 0].astype(np.int64)
lo = f[:, :, 1].astype(np.int64)
rgb = np.dstack([hi & 0xF8, ((hi & 0x07) << 5) | ((lo & 0xE0) >> 3), (lo & 0x1F) << 3]).astype(np.uint8)
print("=== RAW camera frame, luma as ASCII ===")
print(ascii_art(rgb.mean(axis=2)))
print()

# ---- 2) same, 3x3 median filtered ----
def med3(a):
    p = np.pad(a, 1, mode='edge')
    st = np.stack([p[dy:dy + a.shape[0], dx:dx + a.shape[1]] for dy in range(3) for dx in range(3)], axis=0)
    return np.median(st, axis=0)
rgbm = med3(rgb).astype(np.uint8)
print("=== RAW camera frame, 3x3 MEDIAN filtered ===")
print(ascii_art(rgbm.mean(axis=2)))
print()

# ---- 3) JPEG stream frame ----
data = open(r'D:\Software\kanhu\remote-care-box\stream_final.bin', 'rb').read()
jfs = []
p = 0
while True:
    a = data.find(b'\xff\xd8', p)
    if a < 0:
        break
    b = data.find(b'\xff\xd9', a)
    if b < 0:
        break
    jfs.append(data[a:b + 2])
    p = b + 2
jim = np.asarray(Image.open(io.BytesIO(jfs[0])).convert('RGB')).astype(np.uint8)
print("=== JPEG stream frame, luma as ASCII ===")
print(ascii_art(jim.mean(axis=2)))
