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

def dec_rgb565(f):
    hi = f[:, :, 0].astype(np.int64)
    lo = f[:, :, 1].astype(np.int64)
    return np.dstack([hi & 0xF8, ((hi & 0x07) << 5) | ((lo & 0xE0) >> 3), (lo & 0x1F) << 3]).astype(np.float64)

dec = np.array([dec_rgb565(f) for f in frames])
print("=== decode THEN average (correct order) ===")
for k in range(len(dec)):
    print("  frame%d laplacian %.2f" % (k, lapstd(dec[k].mean(axis=2))))
avg = dec.mean(axis=0)
print("  AVERAGE laplacian %.2f" % lapstd(avg.mean(axis=2)))
Image.fromarray(avg.astype(np.uint8)).resize((960, 720), Image.LANCZOS).save(r'D:\Software\kanhu\remote-care-box\raw_avg.png')

print("\n=== lo byte structure ===")
L = frames[:, :, :, 1].astype(np.int64)
H = frames[:, :, :, 0].astype(np.int64)
print("  frac (lo & 0x1F)==0x1F : %.3f" % np.mean((L & 0x1F) == 0x1F))
print("  frac (hi & 0x07)==0x07 : %.3f" % np.mean((H & 0x07) == 0x07))
print("  lo upper3 (lo>>5) values seen:", np.unique(L >> 5).tolist())
print("  hi upper5 (hi>>3) range:", int(H.min() >> 3), int(H.max() >> 3))

print("\n=== per-frame: is hi identical across frames at same pixel? ===")
same_hi = sum(1 for x in range(len(H)) for y in range(240) for z in range(0, 320, 37) if H[x, y, z] == H[0, y, z])
tot = len(H) * 240 * 9
print("  hi identical to frame0: %.3f" % (same_hi / tot))
same_lo = sum(1 for x in range(len(L)) for y in range(240) for z in range(0, 320, 37) if L[x, y, z] == L[0, y, z])
print("  lo identical to frame0: %.3f" % (same_lo / tot))
