import numpy as np, glob
from PIL import Image

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
d = open(fs[0], 'rb').read()
i = d.find(b'\r\n\r\n')
buf = d[i + 4:i + 4 + 153600]

flat = np.frombuffer(buf, dtype=np.uint8)

print("=== global byte structure ===")
print("unique byte values:", len(np.unique(flat)))
print("lo (odd idx) low-5-bits unique:", np.unique(flat[1::2] & 0x1F).tolist())
print("lo (odd idx) unique count:", len(np.unique(flat[1::2])))
print("hi (even idx) low-3-bits unique:", np.unique(flat[0::2] & 0x07).tolist())

def lapstd(g):
    g = np.asarray(g, dtype=np.float64)
    return float((g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1]
                  - g[1:-1, :-2] - g[1:-1, 2:]).std())

H = flat[0::2].reshape(240, 320).astype(np.int64)
L = flat[1::2].reshape(240, 320).astype(np.int64)

def dec_rgb565(hi, lo):
    return np.dstack([hi & 0xF8, ((hi & 0x07) << 5) | ((lo & 0xE0) >> 3), (lo & 0x1F) << 3])

def dec_yuyv(hi, lo):
    y = hi[:, 0::2].astype(np.float64)
    u = lo[:, 0::2].astype(np.float64) - 128
    v = lo[:, 1::2].astype(np.float64) - 128
    Y = y[:, :, None]
    return np.clip(np.dstack([Y + 1.402 * v[:, :, None],
                              Y - 0.344136 * u[:, :, None] - 0.714136 * v[:, :, None],
                              Y + 1.772 * u[:, :, None]]), 0, 255)

def dec_uyvy(hi, lo):
    y = hi[:, 1::2].astype(np.float64)
    u = lo[:, 0::2].astype(np.float64) - 128
    v = lo[:, 1::2].astype(np.float64) - 128
    Y = y[:, :, None]
    return np.clip(np.dstack([Y + 1.402 * v[:, :, None],
                              Y - 0.344136 * u[:, :, None] - 0.714136 * v[:, :, None],
                              Y + 1.772 * u[:, :, None]]), 0, 255)

print("\n=== decode candidates (laplacian std; LOWER = cleaner) ===")
cands = {
    "RGB565 hi/lo": dec_rgb565(H, L),
    "RGB565 swap lo/hi": dec_rgb565(L, H),
    "YUYV": dec_yuyv(H, L),
    "UYVY": dec_uyvy(H, L),
}
for k, v in cands.items():
    print("  %-20s laplacian %.2f" % (k, lapstd(v.mean(axis=2))))

print("\n=== byte-shift search (RGB565 big-endian) ===")
best = []
for off in range(4):
    b = flat[off:]
    n = (len(b) // 2) // 320 * 320
    h = b[:n].reshape(-1, 2)[:, 0].reshape(-1, 320).astype(np.int64)
    l = b[:n].reshape(-1, 2)[:, 1].reshape(-1, 320).astype(np.int64)
    s = lapstd(dec_rgb565(h, l).mean(axis=2))
    best.append((off, s))
    print("  offset %d: laplacian %.2f" % (off, s))

print("\n=== vertical: is corruption row-aligned? ===")
hl = np.abs(np.diff(H.astype(np.float64), axis=1)).mean(axis=1)
ll = np.abs(np.diff(L.astype(np.float64), axis=1)).mean(axis=1)
print("  rows with hi adjacent diff > 40:", int((hl > 40).sum()), "/240")
print("  rows with lo adjacent diff > 40:", int((ll > 40).sum()), "/240")
print("  hi per-row diff (first 30):", [int(x) for x in hl[:30]])
print("  lo per-row diff (first 30):", [int(x) for x in ll[:30]])
