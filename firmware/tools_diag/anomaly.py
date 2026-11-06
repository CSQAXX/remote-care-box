import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'

def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    i = d.find(b'\r\n\r\n')
    return np.frombuffer(d[i+4:i+4+153600], dtype=np.uint8).reshape(240, 320, 2)

def ac(P, axis=1):
    p = P.astype(float)
    if axis == 1:
        p = p.reshape(-1)
        return float(np.corrcoef(p[:-1], p[1:])[0,1])
    return 0.0

f = load('raw3_1.bin')
A = f[:,:,0].astype(np.int64)
B = f[:,:,1].astype(np.int64)

R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
Gfull = (G3 << 3) | G2

print('=== 1) Is G3 and G2 the SAME green channel? (converter computes 6-bit green) ===')
print('   corr(G3, G2)          = %.3f' % np.corrcoef(G3.ravel(), G2.ravel())[0,1])
print('   corr(G3, G2 of next px)= %.3f' % np.corrcoef(G3.ravel()[:-1], G2.ravel()[1:])[0,1])
print('   corr(G3, G2 prev px)   = %.3f' % np.corrcoef(G3.ravel()[1:], G2.ravel()[:-1])[0,1])
print('   corr(G3, Gfull)       = %.3f' % np.corrcoef(G3.ravel(), Gfull.ravel())[0,1])
print('   corr(R5, G3)          = %.3f' % np.corrcoef(R5.ravel(), G3.ravel())[0,1])
print('   corr(R5, G2)          = %.3f' % np.corrcoef(R5.ravel(), G2.ravel())[0,1])
print('   corr(B5, G2)          = %.3f' % np.corrcoef(B5.ravel(), G2.ravel())[0,1])
print('   corr(B5, G3)          = %.3f' % np.corrcoef(B5.ravel(), G3.ravel())[0,1])

print()
print('=== 2) Compare with a REAL RGB565 image: correlation of R and B channels ===')
print('   corr(R5,B5)=%.3f  -> R,B strongly correlated = natural scene (luma dominated)' % np.corrcoef(R5.ravel(), B5.ravel())[0,1])

print()
print('=== 3) TEMPORAL: is G2 garbage stable across frames or re-randomized? ===')
fr = [load(f) for f in ('raw3_1.bin', 'raw3_2.bin')]
G2a = (fr[0][:,:,1] >> 5).astype(np.int64)
G2b = (fr[1][:,:,1] >> 5).astype(np.int64)
R5a = (fr[0][:,:,0] >> 3).astype(np.int64)
R5b = (fr[1][:,:,0] >> 3).astype(np.int64)
print('   corr(G2 frame0, G2 frame1) = %.3f' % np.corrcoef(G2a.ravel(), G2b.ravel())[0,1])
print('   corr(R5 frame0, R5 frame1) = %.3f' % np.corrcoef(R5a.ravel(), R5b.ravel())[0,1])
print('   exact match frac G2        = %.3f' % (G2a == G2b).mean())

print()
print('=== 4) Spatial pattern of G2 error: is it correlated with a shifted copy of G3? ===')
# if converter shifted/garbled bits, maybe G2 = some bit-permuted version of another field
for nm, P in (('R5', R5), ('G3', G3), ('B5', B5), ('A>>5', A >> 5), ('B&31', B & 31)):
    best = None
    for shift in range(-3, 4):
        if shift == 0:
            q = P
        elif shift > 0:
            q = np.roll(P, shift, axis=1)
        else:
            q = np.roll(P, shift, axis=1)
        c = np.corrcoef(q.ravel(), G2.ravel())[0,1]
        if best is None or abs(c) > abs(best[1]):
            best = (shift, c)
    print('   G2 vs %-6s best shift %+d corr %.3f' % (nm, best[0], best[1]))

print()
print('=== 5) What if the LOW BYTE of every OTHER pixel is misaligned (half-pixel shift)? ===')
# If lo bytes were shifted by one pixel relative to hi bytes, then G2/B5 would be misaligned
for sh in (-2, -1, 0, 1, 2):
    Bs = np.roll(B, sh, axis=1)
    g2 = Bs >> 5; b5 = Bs & 31
    a = np.corrcoef(g2.ravel(), (A>>3).ravel())[0,1]
    c = np.corrcoef(b5.ravel(), (A>>3).ravel())[0,1]
    print('   lo shifted %+d : corr(G2,R5) %+.3f  corr(B5,R5) %+.3f' % (sh, a, c))
