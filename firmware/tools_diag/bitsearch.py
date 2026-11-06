import numpy as np, itertools

OUT = r'D:\Software\kanhu\remote-care-box'

def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    i = d.find(b'\r\n\r\n')
    return np.frombuffer(d[i+4:i+4+153600], dtype=np.uint8).reshape(240, 320, 2)

def sm(P):
    C = P[1:-1, 1:-1]
    nb = np.stack([P[1:-1,:-2], P[1:-1,2:], P[:-2,1:-1], P[2:,1:-1]])
    return float(np.abs(C - np.median(nb, axis=0)).mean())

def ac1(P):
    p = P.astype(float).ravel()
    return float(np.corrcoef(p[:-1], p[1:])[0,1])

f = load('raw3_1.bin')
A = f[:,:,0].astype(np.int64)
B = f[:,:,1].astype(np.int64)

def fields(hi, lo):
    R5 = hi >> 3; G3 = hi & 7; G2 = lo >> 5; B5 = lo & 31
    return {'R5': R5, 'G3': G3, 'G2': G2, 'B5': B5}

def score(hi, lo, name):
    d = fields(hi, lo)
    s = {k: (round(sm(v), 2), round(ac1(v), 3)) for k, v in d.items()}
    worst = max(sm(v) for v in d.values())
    worstac = min(ac1(v) for v in d.values())
    print('%-26s worst_smooth %5.2f  worst_ac %.3f   %s' % (name, worst, worstac, s))
    return worst, worstac

print('=== baseline ===')
score(A, B, 'A=hi B=lo (as-is)')
score(B, A, 'B=hi A=lo (swapped)')

print('\n=== rotate odd byte by k (bit rotation) ===')
for k in range(1, 8):
    Bo = ((B << k) | (B >> (8 - k))) & 0xFF
    score(A, Bo, 'rot odd by %d' % k)

print('\n=== reverse odd byte ===')
Brev = np.zeros_like(B)
for bit in range(8):
    Brev |= ((B >> bit) & 1) << (7 - bit)
score(A, Brev, 'odd reversed')

print('\n=== try odd byte as single-bit-plane (each bit alone as 0/1 => LSB/MSB fields) ===')
for bit in range(8):
    print('  odd bit%d: ac1 %.3f  sm %.3f' % (bit, ac1((B>>bit)&1), sm((B>>bit)&1)))

print('\n=== search: partition odd byte bits into two fields, find all smooth splits ===')
best = []
for mask_hi in range(1, 256):
    if bin(mask_hi).count('1') > 4: continue
    # field1 = mask_hi bits, field2 = remaining
    m2 = 0xFF ^ mask_hi
    if m2 == 0: continue
    P1 = (B & mask_hi); P2 = (B & m2)
    # normalize by popcount for comparability
    a1 = ac1(P1); a2 = ac1(P2)
    best.append((min(a1,a2), mask_hi, round(a1,3), round(a2,3)))
best.sort(reverse=True)
for s, m, a1, a2 in best[:8]:
    print('   mask %s -> fields ac1 %.3f / %.3f' % (hex(m), a1, a2))
