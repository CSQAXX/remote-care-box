import numpy as np, glob
from PIL import Image

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
frames = []
for f in fs:
    d = open(f, 'rb').read()
    i = d.find(b'\r\n\r\n')
    frames.append(np.frombuffer(d[i + 4:i + 4 + 153600], dtype=np.uint8).reshape(240, 320, 2))
frames = np.array(frames)

def lapstd(g):
    g = np.asarray(g, dtype=np.float64)
    return float((g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1]
                  - g[1:-1, :-2] - g[1:-1, 2:]).std())

def dec_rgb565(hi, lo):
    return np.dstack([hi & 0xF8, ((hi & 0x07) << 5) | ((lo & 0xE0) >> 3), (lo & 0x1F) << 3])

H = frames[:, :, :, 0].astype(np.int64)
L = frames[:, :, :, 1].astype(np.int64)

print("=== per-frame laplacian (RGB565 decode) ===")
for k in range(len(frames)):
    print("  frame%d laplacian %.2f" % (k, lapstd(dec_rgb565(H[k], L[k]).mean(axis=2))))

avg = frames.astype(np.float64).mean(axis=0)
Havg = avg[:, :, 0].astype(np.int64)
Lavg = avg[:, :, 1].astype(np.int64)
print("AVERAGE of %d frames laplacian %.2f" % (len(frames), lapstd(dec_rgb565(Havg, Lavg).mean(axis=2))))

print("\n=== is lo-noise random per frame? (lo at pixel (100,120)) ===")
for k in range(len(frames)):
    print("  f%d hi=%d lo=%d" % (k, H[k, 100, 120], L[k, 100, 120]))

print("\n=== average lo low-5-bits distribution ===")
print("  lo&0x1F unique:", np.unique(Lavg & 0x1F).tolist())
print("  hi&0x07 unique:", np.unique(Havg & 0x07).tolist())

print("\n=== spatial pattern of lo: is it column-striped? ===")
colvar = L[:, :, :].std(axis=0).mean(axis=0)
print("  per-column std of lo (first 40):", [int(x) for x in colvar[:40]])
rowvar = L[:, :, :].std(axis=0).mean(axis=1)
print("  per-row std of lo (first 40):", [int(x) for x in rowvar[:40]])
