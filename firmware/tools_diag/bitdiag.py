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
f0, f1 = raw[0], raw[1]

def cc(a, b):
    a = a.ravel().astype(np.float64); b = b.ravel().astype(np.float64)
    a -= a.mean(); b -= b.mean()
    d = np.sqrt((a*a).sum()*(b*b).sum())
    return float((a*b).sum()/d) if d else 0.0

print("=== PER-BIT-POSITION temporal correlation (frame0 vs frame1) ===")
print("    (bit 0 = LSB of byte0, bit 8 = LSB of byte1, ...)")
print("    In a correctly-aligned RGB565 stream, all 16 should be similarly correlated.\n")
b0 = np.unpackbits(f0)
b1 = np.unpackbits(f1)
for bit in range(16):
    c = cc(b0[bit::16], b1[bit::16])
    ones = b0[bit::16].mean()
    bar = '#' * int(40 * abs(c))
    print("  bit %2d: corr %+.3f  (ones %.3f) %s" % (bit, c, ones, bar))

print("\n=== per-byte-position correlation for even/odd bytes ===")
for pos in range(4):
    a = f0[pos::2].astype(np.float64); b = f1[pos::2].astype(np.float64)
    print("  bytepos %d (every 2nd byte): corr %+.3f" % (pos, cc(a, b)))

print("\n=== EXHAUSTIVE: find (bit_shift, byte_offset) maximizing MIN channel temporal corr ===")
best = []
for shift in range(16):
    s0 = np.packbits(np.roll(b0, shift))[:153600]
    s1 = np.packbits(np.roll(b1, shift))[:153600]
    for boff in range(4):
        x0 = s0[boff:]; x1 = s1[boff:]
        n = 240*320
        A0 = x0[:n*2].reshape(240,320,2)[:,:,0].astype(np.int64)
        A1 = x1[:n*2].reshape(240,320,2)[:,:,0].astype(np.int64)
        B0 = x0[:n*2].reshape(240,320,2)[:,:,1].astype(np.int64)
        B1 = x1[:n*2].reshape(240,320,2)[:,:,1].astype(np.int64)
        cR = cc(A0>>3, A1>>3)
        cG = cc((A0&7)*8 + (B0>>5), (A1&7)*8 + (B1>>5))
        cB = cc(B0&0x1F, B1&0x1F)
        best.append((min(cR,cG,cB), shift, boff, cR, cG, cB))
best.sort(reverse=True)
print("  rank  minCorr  shift  byteoff     cR      cG      cB")
for m, s, bo, cR, cG, cB in best[:10]:
    print("        %+.3f    %2d      %d      %+.3f  %+.3f  %+.3f" % (m, s, bo, cR, cG, cB))
