import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


f = load('raw3_1.bin')
A = f[:, :, 0].astype(np.int64)
B = f[:, :, 1].astype(np.int64)
R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31

print('=== ROW 120, first 24 pixels ===')
print(' x   hi lo | R5 G3 |G2 B5')
for x in range(24):
    print(' %2d  %02x  %02x | %2d  %d | %d  %d' % (
        x, A[120, x], B[120, x], R5[120, x], G3[120, x], G2[120, x], B5[120, x]))

print()
print('=== row 120: low byte top-3 bits sequence ===')
print('G2:', ' '.join('%d' % v for v in G2[120, :60]))
print('B5:', ' '.join('%d' % v for v in B5[120, :60]))
print('R5:', ' '.join('%d' % v for v in R5[120, :60]))
print('G3:', ' '.join('%d' % v for v in G3[120, :60]))

print()
print('=== row 121 and 122 for comparison ===')
for r in (121, 122):
    print('r%d G2:' % r, ' '.join('%d' % v for v in G2[r, :60]))

print()
print('=== where are the G2 discontinuities? (per row, count of |G2[x+1]-G2[x]|>3) ===')
for r in (119, 120, 121, 122):
    d = np.abs(np.diff(G2[r]))
    print('  row %d: %d / %d jumps >3' % (r, (d > 3).sum(), d.size))

print()
print('=== vertical profile of G2 at a few columns ===')
for x in (0, 10, 50, 100):
    print('  col %d G2[0:40]:' % x, ' '.join('%d' % v for v in G2[0:40, x]))

print()
print('=== is G2 constant along a row? check row-wise variance ===')
vr = G2.var(axis=1)
print('  row 118..126 var:', np.round(vr[118:127], 3).tolist())
print('  overall: rows with var<0.5: %d / 240' % (vr < 0.5).sum())
print('  mean row var %.3f' % vr.mean())
