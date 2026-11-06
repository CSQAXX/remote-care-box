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
        out.append(np.frombuffer(body[:153600], dtype=np.uint8))
    return out

raw = load(r'D:\Software\kanhu\remote-care-box\raw3_*.bin')
print("frames:", len(raw))
f0, f1 = raw[0], raw[1]

def cc(a, b):
    a = a.ravel().astype(np.float64); b = b.ravel().astype(np.float64)
    a -= a.mean(); b -= b.mean()
    d = np.sqrt((a*a).sum()*(b*b).sum())
    return float((a*b).sum()/d) if d else 0.0

def shift_bits(stream, s):
    bits = np.unpackbits(stream)
    bits = np.roll(bits, s)
    return np.packbits(bits)

def decode(b16, w=320, h=240):
    """b16: big-endian 16-bit words -> RGB565 -> RGB888"""
    n = h * w
    b = b16[:n*2].reshape(h, w, 2)
    hi = b[:, :, 0].astype(np.int64); lo = b[:, :, 1].astype(np.int64)
    R = ((hi >> 3) & 0x1F) << 3
    G = (((hi & 7) << 3) | ((lo >> 5) & 7))
    B = ((lo & 0x1F) << 3)
    return R, G, B

print("\n=== BIT-SHIFT SEARCH (DVP bit misalignment) ===")
print("  score = temporal corr(R) + temporal corr(B) + spatial smoothness")
results = []
for s in range(16):
    s0 = shift_bits(f0, s)
    s1 = shift_bits(f1, s)
    R0, G0, B0 = decode(s0)
    R1, G1, B1 = decode(s1)
    # temporal correlation of each channel
    tR = cc(R0, R1); tG = cc(G0, G1); tB = cc(B0, B1)
    # spatial smoothness (adjacent diff)
    aR = float(np.abs(R0[:,1:]-R0[:,:-1]).mean())
    aG = float(np.abs(G0[:,1:]-G0[:,:-1]).mean())
    aB = float(np.abs(B0[:,1:]-B0[:,:-1]).mean())
    score = (tR + tG + tB) - (aR + aG + aB)/100.0
    results.append((s, tR, tG, tB, aR, aG, aB, score))
    print("  shift %2d: tempR %+.3f tempG %+.3f tempB %+.3f | adjR %6.2f adjG %6.2f adjB %6.2f | score %+.3f"
          % (s, tR, tG, tB, aR, aG, aB, score))
results.sort(key=lambda x: -x[-1])
best = results[0]
print("\n  >>> BEST bit shift = %d (score %+.3f)" % (best[0], best[-1]))
s = best[0]
R, G, B = decode(shift_bits(f0, s))
Image.fromarray(np.dstack([R, G, B]).astype(np.uint8)).resize((960,720), Image.NEAREST).save(
    r'D:\Software\kanhu\remote-care-box\bitshift_best.png')
print("  saved bitshift_best.png")
