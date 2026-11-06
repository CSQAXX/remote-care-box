import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


def ac1(P):
    """horizontal neighbour correlation, per-row then averaged (no row-spanning)"""
    P = P.astype(float)
    c = []
    for r in range(P.shape[0]):
        row = P[r]
        c.append(np.corrcoef(row[:-1], row[1:])[0, 1])
    return float(np.mean(c))


def ac1_flat(P):
    p = P.astype(float).ravel()
    return float(np.corrcoef(p[:-1], p[1:])[0, 1])


def vert(P):
    P = P.astype(float)
    c = []
    for r in range(P.shape[1]):
        col = P[:, r]
        c.append(np.corrcoef(col[:-1], col[1:])[0, 1])
    return float(np.mean(c))


files = ['raw3_1.bin', 'raw3_2.bin', 'raw_0.bin', 'h_http_0.bin', 'h_conv0_0.bin']

print('%-14s | %-22s | %-22s | %-22s' % ('file', 'HORIZ ac1 (per-row)', 'HORIZ ac1 (flat)', 'VERT ac1'))
print('%-14s | %7s %7s %7s %7s | %7s %7s %7s %7s | %7s %7s %7s %7s' % (
    '', 'R5', 'G3', 'G2', 'B5', 'R5', 'G3', 'G2', 'B5', 'R5', 'G3', 'G2', 'B5'))
for fn in files:
    try:
        f = load(fn)
    except Exception as e:
        print(fn, 'ERR', e); continue
    A = f[:, :, 0].astype(np.int64); B = f[:, :, 1].astype(np.int64)
    F = {'R5': A >> 3, 'G3': A & 7, 'G2': B >> 5, 'B5': B & 31}
    h = [ac1(F[k]) for k in ('R5', 'G3', 'G2', 'B5')]
    hf = [ac1_flat(F[k]) for k in ('R5', 'G3', 'G2', 'B5')]
    v = [vert(F[k]) for k in ('R5', 'G3', 'G2', 'B5')]
    print('%-14s | %7.3f %7.3f %7.3f %7.3f | %7.3f %7.3f %7.3f %7.3f | %7.3f %7.3f %7.3f %7.3f' % (
        fn, h[0], h[1], h[2], h[3], hf[0], hf[1], hf[2], hf[3], v[0], v[1], v[2], v[3]))

print()
print('=== KEY: adjacent-pixel |difference| per channel (mean) ===')
print('%-14s | %7s %7s %7s %7s' % ('file', 'R5', 'G3', 'G2', 'B5'))
for fn in files:
    try:
        f = load(fn)
    except Exception:
        continue
    A = f[:, :, 0].astype(np.int64); B = f[:, :, 1].astype(np.int64)
    F = {'R5': A >> 3, 'G3': A & 7, 'G2': B >> 5, 'B5': B & 31}
    ds = []
    for k in ('R5', 'G3', 'G2', 'B5'):
        P = F[k]
        ds.append(np.abs(np.diff(P, axis=1)).mean())
    print('%-14s | %7.2f %7.2f %7.2f %7.2f' % (fn, ds[0], ds[1], ds[2], ds[3]))
