import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'

def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    i = d.find(b'\r\n\r\n')
    return np.frombuffer(d[i+4:i+4+153600], dtype=np.uint8).reshape(240, 320, 2)

f = load('raw3_1.bin')
A = f[:,:,0].astype(np.int64)
B = f[:,:,1].astype(np.int64)
R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31

print('=== distribution ===')
for n, P in (('R5',R5),('G3',G3),('G2',G2),('B5',B5)):
    h = np.bincount(P.ravel(), minlength=32)[:32]
    print('  %-3s' % n, ' '.join('%3d' % x for x in h[:16]))
print('  G3 nonzero frac %.3f  G2 nonzero frac %.3f' % ((G3>0).mean(), (G2>0).mean()))

print()
print('=== is G2 a simple function of the other 3 fields? ===')
cands = {
    'R5&7': R5 & 7, 'B5&7': B5 & 7, '(R5+B5)&7': (R5+B5)&7, '(R5-B5)&7': (R5-B5)&7,
    '(R5^B5)&7': (R5^B5)&7, '(G3+B5)&7': (G3+B5)&7, '(G3+R5)&7': (G3+R5)&7,
    'B5>>2': B5>>2, 'R5>>2': R5>>2, '(B5+G3)&7': (B5+G3)&7,
    '(R5+G3+B5)&7': (R5+G3+B5)&7, 'B5>>3': B5>>3, 'R5>>3': R5>>3,
}
for n, C in cands.items():
    print('   G2 == %-14s exact %.3f' % (n, (C == G2).mean()))

print()
print('=== is G2 == green of a DIFFERENT pixel (spatial shift)? ===')
for sh in (-3,-2,-1,0,1,2,3):
    G3s = np.roll(G3, sh, axis=1)
    for combo, C in (('G3', G3s), ('R5', np.roll(R5,sh,axis=1)), ('B5', np.roll(B5,sh,axis=1))):
        m = (C == G2).mean()
        if m > 0.05:
            print('   shift %+d G2==%s exact %.3f' % (sh, combo, m))

print()
print('=== GRADIENT check: G3 smooth vs sawtooth ===')
row = G3[100, :60]
print('   G3 row100[0:60]:', ' '.join('%d' % v for v in row))
row2 = B5[100, :60]
print('   B5 row100[0:60]:', ' '.join('%d' % v for v in row2))
row3 = R5[100, :60]
print('   R5 row100[0:60]:', ' '.join('%d' % v for v in row3))
print('   G2 row100[0:60]:', ' '.join('%d' % v for v in G2[100, :60]))
