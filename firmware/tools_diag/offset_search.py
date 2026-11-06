import numpy as np, glob
from PIL import Image

def load(pat):
    out = []
    for f in sorted(glob.glob(pat)):
        d = open(f, 'rb').read()
        i = d.find(b'\r\n\r\n')
        body = d[i + 4:]
        if len(body) < 153600:
            continue
        out.append(np.frombuffer(body[:153600], dtype=np.uint8).reshape(240, 320, 2))
    return np.array(out)

fr = load(r'D:\Software\kanhu\remote-care-box\raw3_*.bin')
print("frames:", len(fr))
flat = fr[0].reshape(-1)

def dec_bytes(buf, w=320, h=240):
    need = h * w * 2
    b = buf[:need].reshape(h, w, 2)
    hi = b[:, :, 0].astype(np.int64); lo = b[:, :, 1].astype(np.int64)
    return np.dstack([hi & 0xF8, ((hi & 7) << 5) | ((lo & 0xE0) >> 3), (lo & 0x1F) << 3]).astype(np.float64)

def lap(g):
    g = np.asarray(g, dtype=np.float64)
    return float((g[1:-1, 1:-1]*4 - g[:-2, 1:-1] - g[2:, 1:-1]
                  - g[1:-1, :-2] - g[1:-1, 2:]).std())

print("\n=== BRUTE FORCE: byte offset in the whole stream ===")
res = []
for off in range(0, 16):
    b = flat[off:]
    need = 240 * 320 * 2
    if len(b) < need:
        continue
    v = lap(dec_bytes(b[:need]).mean(axis=2))
    res.append((off, v))
    print("  byte offset %2d: laplacian %8.2f" % (off, v))
res.sort(key=lambda x: x[1])
print("  BEST:", res[0])

print("\n=== BRUTE FORCE: PIXEL offset ===")
res2 = []
for poff in range(-8, 9):
    b = np.roll(flat, poff * 2)
    v = lap(dec_bytes(b[:240 * 320 * 2]).mean(axis=2))
    res2.append((poff, v))
    print("  pixel offset %+3d: laplacian %8.2f" % (poff, v))
res2.sort(key=lambda x: x[1])
print("  BEST:", res2[0])

best_off = res[0][0]
img = dec_bytes(flat[best_off:best_off + 240 * 320 * 2])
Image.fromarray(img.astype(np.uint8)).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\best_offset.png')
print("saved best_offset.png (offset %d)" % best_off)

print("\n=== CHANNEL CROSS-CORRELATION in current alignment ===")
b0 = fr[0]
hi = b0[:, :, 0].astype(np.float64); lo = b0[:, :, 1].astype(np.float64)
R = (hi >> 3) & 0x1F
B = (lo & 0x1F).astype(np.float64)
print("  corr(R[x], B[x])   = %.3f" % np.corrcoef(R[:, :-1].ravel(), B[:, :-1].ravel())[0, 1])
print("  corr(R[x], B[x+1]) = %.3f" % np.corrcoef(R[:, :-1].ravel(), B[:, 1:].ravel())[0, 1])
print("  corr(R[x], B[x-1]) = %.3f" % np.corrcoef(R[:, 1:].ravel(), B[:, :-1].ravel())[0, 1])
print("  corr(R[x], B[x+2]) = %.3f" % np.corrcoef(R[:, :-2].ravel(), B[:, 2:].ravel())[0, 1])
