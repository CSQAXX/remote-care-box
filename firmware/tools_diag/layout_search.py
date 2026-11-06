import numpy as np
from itertools import permutations

OUT = r'D:\Software\kanhu\remote-care-box'

def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    i = d.find(b'\r\n\r\n')
    return np.frombuffer(d[i+4:i+4+153600], dtype=np.uint8).reshape(240, 320, 2)

def ac1(P):
    p = P.astype(float).ravel()
    return float(np.corrcoef(p[:-1], p[1:])[0, 1])

def sm(P):
    C = P[1:-1, 1:-1]
    nb = np.stack([P[1:-1,:-2], P[1:-1,2:], P[:-2,1:-1], P[2:,1:-1]])
    return float(np.abs(C - np.median(nb, axis=0)).mean())

f = load('raw3_1.bin')
words = ((f[:,:,0].astype(np.int64) << 8) | f[:,:,1].astype(np.int64)).ravel()

def bits(v, hi, lo):
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)

# candidate 3-field contiguous layouts inside the 16-bit word
layouts = []
# (name, r(hi,lo), g(hi,lo), b(hi,lo))
def add(n, r, g, b):
    layouts.append((n, r, g, b))

add('RGB565',      (15,11), (10,5),  (4,0))
add('BGR565',      (4,0),   (10,5),  (15,11))
add('RGB555',      (15,11), (10,6),  (5,1))
add('BGR555',      (5,1),   (10,6),  (15,11))
add('RGB565-LSB',  (4,0),   (10,5),  (15,11))
add('GRB565',      (10,5),  (15,11), (4,0))
add('GBR565',      (10,5),  (4,0),   (15,11))
add('RBG565',      (15,11), (4,0),   (10,5))
add('BRG565',      (4,0),   (15,11), (10,5))
add('YUV444_888', (15,8),  (7,0),   (15,8))   # degenerate, skip
# 8/8/8 candidates with byte split variations
add('RGB888-hi',   (23,16), (15,8),  (7,0))    # nonsense but harmless

print('%-12s %-28s %-28s %-28s  %s' % ('layout', 'field ac1 (R,G,B)', 'field smooth', 'inter-channel corr', 'verdict'))
rows = []
for name, r, g, b in layouts:
    if name == 'YUV444_888':
        continue
    try:
        R = bits(words, r[0], r[1]); G = bits(words, g[0], g[1]); Bc = bits(words, b[0], b[1])
    except Exception:
        continue
    if R.max() > 255 or G.max() > 255 or Bc.max() > 255:
        continue
    a = (ac1(R), ac1(G), ac1(Bc))
    s = (sm(R.reshape(240,320)), sm(G.reshape(240,320)), sm(Bc.reshape(240,320)))
    c = (np.corrcoef(R.ravel(), G.ravel())[0,1], np.corrcoef(R.ravel(), Bc.ravel())[0,1],
         np.corrcoef(G.ravel(), Bc.ravel())[0,1])
    worst_ac = min(a)
    worst_sm = max(s)
    min_c = min(abs(c[0]), abs(c[1]), abs(c[2]))
    rows.append((worst_ac, worst_sm, min_c, name, a, s, c))

rows.sort(key=lambda r: (-r[0], r[1]))
for worst_ac, worst_sm, min_c, name, a, s, c in rows:
    print('%-12s  %.3f %.3f %.3f    %6.2f %6.2f %6.2f    %.3f %.3f %.3f   minAC %.3f minC %.3f' % (
        name, a[0], a[1], a[2], s[0], s[1], s[2], c[0], c[1], c[2], worst_ac, min_c))

print()
print('=== brute force: ALL contiguous 3-field splits of the 16-bit word ===')
best = []
for r0 in range(15, 2, -1):
    for g0 in range(r0 - 1, 2, -1):
        for b0 in range(g0 - 1, 1, -1):
            # fields: r = bits r0..15, g = bits g0..r0-1, b = bits b0..g0-1
            R = bits(words, 15, r0)
            G = bits(words, r0 - 1, g0)
            Bc = bits(words, g0 - 1, b0)
            if R.max() > 255 or G.max() > 255 or Bc.max() > 255:
                continue
            a = (ac1(R), ac1(G), ac1(Bc))
            c = (abs(np.corrcoef(R.ravel(), G.ravel())[0,1]),
                 abs(np.corrcoef(R.ravel(), Bc.ravel())[0,1]),
                 abs(np.corrcoef(G.ravel(), Bc.ravel())[0,1]))
            score = min(a) * min(c)
            best.append((score, min(a), min(c), (r0, g0, b0), a, c))
best.sort(reverse=True)
for score, ma, mc, cuts, a, c in best[:10]:
    print('   cuts(hi..lo)=%s  minAC %.3f minCorr %.3f  ac=(%.3f,%.3f,%.3f) corr=(%.3f,%.3f,%.3f)' % (
        cuts, ma, mc, a[0], a[1], a[2], c[0], c[1], c[2]))
