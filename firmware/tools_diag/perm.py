import numpy as np, itertools, sys, time
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def planes(fn, rows=30):
    d = open(OUT + '\\' + fn, 'rb').read()[:153600]
    b = np.frombuffer(d, dtype=np.uint8)[:rows * 640]
    P = np.zeros((8, b.size), dtype=np.int16)
    for i in range(8):
        P[i] = (b >> i) & 1
    return P, b.size


def corr(a, x, y):
    x = x - x.mean(); y = y - y.mean()
    d = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / d) if d > 1e-9 else 0.0


def full_score(P, perm, nbytes):
    """perm[i] = which received bit becomes true bit i"""
    T = np.zeros(nbytes, dtype=np.int32)
    for i in range(8):
        T |= P[perm[i]].astype(np.int32) << i
    TH = T[0::2]
    TL = T[1::2]
    R = (TH >> 3).astype(np.float64)
    G = ((TH & 7) * 8 + (TL >> 5)).astype(np.float64)
    B = (TL & 31).astype(np.float64)
    return corr(R, G, G) + corr(R, B, R) + corr(G, B, G)


stacks = []
for fn in ['m0_0.bin', 'm0_1.bin', 'm0_2.bin', 'm0_3.bin',
           'http_truth_0.bin', 'http_truth_1.bin', 'http_truth_2.bin']:
    stacks.append(planes(fn))

print('=== brute force: 8! DVP bit permutations ===', flush=True)
t0 = time.time()
results = []
for perm in itertools.permutations(range(8)):
    s = sum(full_score(P, perm, n) for P, n in stacks) / len(stacks)
    results.append((s, perm))
results.sort(reverse=True)
print('  took %.1fs' % (time.time() - t0), flush=True)
print()
print('  TOP 10:')
for s, p in results[:10]:
    print('    score=%+.4f  perm=%s' % (s, p))
ident = tuple(range(8))
for s, p in results:
    if p == ident:
        print('  identity (current wiring) score=%+.4f, rank %d/%d'
              % (s, [q for _, q in results].index(ident) + 1, len(results)))
        break
