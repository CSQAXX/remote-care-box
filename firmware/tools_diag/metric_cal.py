import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'

def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    i = d.find(b'\r\n\r\n')
    return np.frombuffer(d[i+4:i+4+153600], dtype=np.uint8).reshape(240, 320, 2)

def med_resid(P, k=1):
    """|P[x] - median(P[x-k], P[x+k])|  per pixel"""
    m = np.median(np.stack([P[:, :-2*k], P[:, 2*k:]]), axis=0)
    full = np.zeros_like(P)
    full[:, k:-k] = np.abs(P[:, k:-k].astype(np.int64) - m.astype(np.int64))
    return full

def report(fn, f):
    A = f[:, :, 0].astype(np.int64); B = f[:, :, 1].astype(np.int64)
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    print('== %s' % fn)
    for nm, P in (('R5', R5), ('G3', G3), ('G2', G2), ('B5', B5)):
        r = med_resid(P)
        core = r[:, 1:-1]
        print('   %-3s resid mean %.3f  >2 %6.2f%%  >3 %6.2f%%  max %d' % (
            nm, core.mean(), (core > 2).mean() * 100, (core > 3).mean() * 100, core.max()))

for fn in ('raw3_1.bin', 'raw3_2.bin', 'raw_0.bin', 'raw2_0.bin'):
    report(fn, load(fn))

print()
print('=== SYNTHETIC CLEAN baseline ===')
h, w = 240, 320
yy, xx = np.mgrid[0:h, 0:w]
base = (yy // 4 * 8 + xx // 8).astype(np.int64)
A_s = (base & 0xF8).astype(np.uint8)
B_s = ((xx // 16 * 4) & 0xFC).astype(np.uint8)
clean = np.dstack([A_s, B_s])
report('synthetic', clean)

print()
print('=== PROPOSED FIRMWARE METRIC ===')
print('green_low3 = (lo>>5)&7  ->  bad_pct = fraction of pixels with')
print('             |green_low3[x] - median(green_low3[x-1], green_low3[x+1])| > 3')
print()
for fn in ('raw3_1.bin', 'raw3_2.bin', 'raw_0.bin', 'raw2_0.bin'):
    f = load(fn)
    G2 = (f[:, :, 1] >> 5).astype(np.int64)
    r = med_resid(G2)[:, 1:-1]
    print('   %-13s bad_pct %6.2f%%   (mean resid %.3f)' % (
        fn, (r > 3).mean() * 100, r.mean()))
G2c = (clean[:, :, 1] >> 5).astype(np.int64)
rc = med_resid(G2c)[:, 1:-1]
print('   %-13s bad_pct %6.2f%%   (mean resid %.3f)' % ('CLEAN', (rc > 3).mean() * 100, rc.mean()))
