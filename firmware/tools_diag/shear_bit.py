import numpy as np, glob
from PIL import Image

def load(pat):
    out = []
    for f in sorted(glob.glob(pat)):
        d = open(f, 'rb').read()
        i = d.find(b'\r\n\r\n')
        body = d[i + 4:]
        if len(body) < 153600:
            print("  skip", f, len(body)); continue
        out.append(np.frombuffer(body[:153600], dtype=np.uint8).reshape(240, 320, 2))
    return out

fr = load(r'D:\Software\kanhu\remote-care-box\raw3_*.bin')
b0, b1 = fr[0], fr[1]
A0 = b0[:,:,0].astype(np.float64); B0 = b0[:,:,1].astype(np.float64)
A1 = b1[:,:,0].astype(np.float64); B1 = b1[:,:,1].astype(np.float64)

def cc(a, b):
    a = a.ravel(); b = b.ravel()
    a = a - a.mean(); b = b - b.mean()
    d = np.sqrt((a*a).sum()*(b*b).sum())
    return float((a*b).sum()/d) if d else 0.0

def lapstd(g):
    l = g[1:-1,1:-1]*4 - g[:-2,1:-1] - g[2:,1:-1] - g[1:-1,:-2] - g[1:-1,2:]
    return float(l.std())

print("=== TEST 1: is plane B just a SHIFTED version of itself across frames? ===")
print("  frame0 vs frame1, same plane, various horizontal shifts:")
for k in range(-4, 5):
    if k >= 0: x, y = A0[:, :-k or None], A1[:, k:]
    else:      x, y = A0[:, -k:], A1[:, :k]
    ca = cc(x, y)
    if k >= 0: x, y = B0[:, :-k or None], B1[:, k:]
    else:      x, y = B0[:, -k:], B1[:, :k]
    cb = cc(x, y)
    print("    shift %+d: corrA %+.3f  corrB %+.3f" % (k, ca, cb))

print("\n=== TEST 2: VERTICAL shear - does row r of frame0 match row r+m of frame1? ===")
for m in range(-4, 5):
    if m >= 0: x, y = A0[m:], A1[:240-m]
    else:      x, y = A0[:240+m], A1[-m:]
    ca = cc(x, y)
    if m >= 0: x, y = B0[m:], B1[:240-m]
    else:      x, y = B0[:240+m], B1[-m:]
    cb = cc(x, y)
    print("    row shift %+d: corrA %+.3f  corrB %+.3f" % (m, ca, cb))

print("\n=== TEST 3: BIT-SHIFT search on the byte stream (DVP bit misalignment) ===")
stream = b0.reshape(-1)
best = []
for s in range(16):
    # shift the whole bit stream by s bits, then take 16-bit big-endian words
    bits = np.unpackbits(stream)
    bits = np.roll(bits, s)
    sh = np.packbits(bits)
    img = sh[:153600].reshape(240, 320, 2)
    a = img[:,:,0].astype(np.float64); bb = img[:,:,1].astype(np.float64)
    R = (a >> 3) & 0x1F; B = (bb & 0x1F)
    gray = (R*0.3 + B*0.1)
    best.append((s, lapstd(gray), cc(R, B)))
    print("    bit shift %2d: lapstd %7.2f  corr(R,B) %+.3f" % best[-1])
best.sort(key=lambda x: -x[1])
print("    -> best by structure:", best[0])

print("\n=== TEST 4: YUYV decode of frame0 (chroma smooth?) ===")
Y = b0[:,:,0].astype(np.float64)
U = b0[:,:,1].astype(np.float64)[:, 0::2]
V = b0[:,:,1].astype(np.float64)[:, 1::2]
def adj(P): return float(np.abs(P[:,1:]-P[:,:-1]).mean())
print("    Y adj %.2f   U adj %.2f   V adj %.2f" % (adj(Y), adj(U), adj(V)))
print("    -> YUYV needs U,V adj << Y adj;  RGB565 needs U,V adj ~ Y adj")
