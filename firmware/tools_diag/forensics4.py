import numpy as np, glob

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
frames = []
for f in fs:
    d = open(f, 'rb').read()
    i = d.find(b'\r\n\r\n')
    frames.append(np.frombuffer(d[i + 4:i + 4 + 153600], dtype=np.uint8).reshape(240, 320, 2))
frames = np.array(frames)
NF = len(frames)

def lapstd(g):
    g = np.asarray(g, dtype=np.float64)
    return float((g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1]
                  - g[1:-1, :-2] - g[1:-1, 2:]).std())

def dec(hi, lo):
    return np.dstack([hi & 0xF8, ((hi & 0x07) << 5) | ((lo & 0xE0) >> 3), (lo & 0x1F) << 3]).astype(np.float64)

print("=== per-channel Laplacian (frame 0) ===")
H = frames[0, :, :, 0].astype(np.int64)
L = frames[0, :, :, 1].astype(np.int64)
im = dec(H, L)
for ci, cn in enumerate("RGB"):
    print("  %s laplacian %.2f" % (cn, lapstd(im[:, :, ci])))

print("\n=== within-row byte shift search (hi fixed, lo shifted) ===")
best = []
for s in range(-6, 7):
    if s == 0:
        continue
    Ls = np.roll(L, s, axis=1)
    ims = dec(H, Ls)
    v = lapstd(ims.mean(axis=2))
    best.append((s, v))
    print("  shift %+d: laplacian %.2f" % (s, v))
print("  shift  0: laplacian %.2f" % lapstd(im.mean(axis=2)))

print("\n=== whole-frame byte shift (global) ===")
flat = frames[0].reshape(-1)
for s in range(-4, 5):
    if s == 0:
        continue
    b = np.roll(flat, s)
    hh = b[0::2][:240 * 320].reshape(240, 320).astype(np.int64)
    ll = b[1::2][:240 * 320].reshape(240, 320).astype(np.int64)
    print("  shift %+d: laplacian %.2f" % (s, lapstd(dec(hh, ll).mean(axis=2))))

print("\n=== per-row temporal noise (row = avg |frame - mean|) ===")
decf = np.array([dec(f[:, :, 0].astype(np.int64), f[:, :, 1].astype(np.int64)) for f in frames])
mean_img = decf.mean(axis=0)
rowerr = np.abs(decf - mean_img).mean(axis=(0, 3, 4))   # per row
print("  row noise (0..239):")
for y in range(0, 240, 4):
    print("    row %3d: %6.2f %s" % (y, rowerr[y], "#" * int(rowerr[y] / 3)))
