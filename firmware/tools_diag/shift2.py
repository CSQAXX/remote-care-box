import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8)


print('=== SHIFT SEARCH: is one byte-stream offset relative to the other? ===')
for fn in ('raw3_1.bin', 'h_http_0.bin'):
    b = load(fn).astype(np.int64)
    hi = b[0::2]
    lo = b[1::2]
    print()
    print('-- %s --' % fn)
    print('  adjacent |diff| of hi stream: %.2f   lo stream: %.2f' % (
        np.abs(np.diff(hi)).mean(), np.abs(np.diff(lo)).mean()))
    best = []
    for sh in range(-8, 9):
        if sh >= 0:
            a = hi[sh:]; c = lo[:len(lo) - sh] if sh else lo
        else:
            a = hi[:sh]; c = lo[-sh:]
        n = min(len(a), len(c))
        if n < 1000:
            continue
        cc = np.corrcoef(a[:n].astype(float), c[:n].astype(float))[0, 1]
        best.append((abs(cc), sh, cc))
    best.sort(reverse=True)
    print('  best hi/lo cross-correlation shifts:', [(sh, round(cc, 3)) for _, sh, cc in best[:4]])

print()
print('=== CROSS-CORRELATION BETWEEN hi AND lo STREAMS (same pixel) ===')
print('For RGB565 from a real image, hi and lo of the SAME pixel correlate.')
for fn in ('raw3_1.bin', 'h_http_0.bin', 'h_conv0_0.bin'):
    b = load(fn).astype(np.int64)
    hi = b[0::2]; lo = b[1::2]
    print('  %-16s corr(hi,lo) = %+.3f' % (fn, np.corrcoef(hi, lo)[0, 1]))

print()
print('=== ROW-LEVEL ANALYSIS: which rows are "wild"? ===')
for fn in ('raw3_1.bin', 'h_http_0.bin'):
    b = load(fn).astype(np.int64).reshape(240, 640)
    hi = b[:, 0::2]; lo = b[:, 1::2]
    dh = np.abs(np.diff(hi, axis=1)).mean(axis=1)
    dl = np.abs(np.diff(lo, axis=1)).mean(axis=1)
    wild_h = (dh > 25).sum(); wild_l = (dl > 25).sum()
    print('  %-16s rows with hi-diff>25: %3d/240   lo-diff>25: %3d/240' % (fn, wild_h, wild_l))
    print('     hi diff per row (every 10th): %s' % np.round(dh[::10], 1).tolist())
    print('     lo diff per row (every 10th): %s' % np.round(dl[::10], 1).tolist())
