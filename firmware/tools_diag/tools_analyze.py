import struct, sys
import numpy as np

W, H = 320, 240
data = open("raw_frame.bin", "rb").read()
assert len(data) == W * H * 2
arr = np.frombuffer(data, dtype=">u2").reshape(H, W)  # big-endian RGB565 as sensor gives

# channel split
r5 = (arr >> 11) & 0x1F
g6 = (arr >> 5) & 0x3F
b5 = arr & 0x1F
g3 = (arr >> 8) & 0x07
g2 = (arr >> 5) & 0x07

def median_resid_bad(ch):
    """count pixels where |ch[x] - median(ch[x-1],ch[x+1])| > thr"""
    thr = 3
    left = ch[:, :-2].astype(np.int32)
    right = ch[:, 2:].astype(np.int32)
    mid = (left + right) // 2
    cur = ch[:, 1:-1].astype(np.int32)
    resid = np.abs(cur - mid)
    return (resid > thr).sum(), resid.size

for name, ch in [("R5", r5), ("G6", g6), ("G3", g3), ("G2", g2), ("B5", b5)]:
    bad, tot = median_resid_bad(ch)
    d = np.abs(np.diff(ch.astype(np.int32), axis=1))
    print(f"{name}: median_resid>3 = {bad*100.0/tot:.2f}%   mean|adj diff| = {d.mean():.3f}")

print()
print("=== row-mean profile (per 16 rows) ===")
rowavg = arr.mean(axis=1)
for y0 in range(0, H, 16):
    print(f"  row {y0:3d}: {rowavg[y0:y0+16].mean():7.2f}")

print()
print("=== 2D block mean map (40x30 blocks) ===")
bm = arr.reshape(30, 8, 40, 8).mean(axis=(1, 3))
for r in range(0, 30, 2):
    print("  " + " ".join(f"{bm[r,c]:5.0f}" for c in range(40)))

print()
print("=== unique pixel values sample ===")
print("  distinct RGB565 values:", len(np.unique(arr)))
print("  min/max:", arr.min(), arr.max())

# save as PNG for visual
try:
    from PIL import Image
    img = Image.fromarray(arr.astype(np.uint16), "I;16").convert("RGB")
    img.save("raw_frame.png")
    print("saved raw_frame.png")
except Exception as e:
    print("png save skipped:", e)
