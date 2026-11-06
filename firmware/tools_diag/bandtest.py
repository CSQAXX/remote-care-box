import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8)


def row_smooth(b, y, shift):
    """adjacent-diff energy of row y parsed with given byte offset (0=hi first)"""
    row = b[y * 640:(y + 1) * 640].astype(np.int64)
    if shift >= 0:
        hi = row[shift::2]
        lo = row[shift + 1::2]
    else:
        hi = row[:shift][::-1][::2]
        lo = row[1 + shift::2]
    n = min(len(hi), len(lo))
    dh = np.abs(np.diff(hi[:n])).mean()
    dl = np.abs(np.diff(lo[:n])).mean()
    return dh, dl


for fn in ('raw3_1.bin', 'h_http_0.bin', 'h_conv0_0.bin'):
    b = load(fn)
    print('== %s ==' % fn)
    print('  row : best shift (by total adjacent-diff)      dh     dl')
    for y in (0, 10, 30, 60, 100, 119, 120, 121, 130, 150, 160, 180, 200, 230):
        best = None
        for sh in (-2, -1, 0, 1, 2):
            dh, dl = row_smooth(b, y, sh)
            tot = dh + dl
            if best is None or tot < best[0]:
                best = (tot, sh, dh, dl)
        print('  %3d : shift %+d   dh %6.2f  dl %6.2f' % (y, best[1], best[2], best[3]))
    print()

print('=== IS THE WILDNESS IN BANDS? (per-row dh for hi stream, shift 0) ===')
for fn in ('h_http_0.bin',):
    b = load(fn).astype(np.int64).reshape(240, 640)
    dh = np.abs(np.diff(b[:, 0::2], axis=1)).mean(axis=1)
    band = (dh > 25).astype(int)
    # find run boundaries
    runs = []
    cur = band[0]; start = 0
    for i in range(1, 240):
        if band[i] != cur:
            runs.append((cur, start, i - 1))
            cur = band[i]; start = i
    runs.append((cur, start, 239))
    print('  %s:' % fn)
    for v, a, z in runs:
        print('    rows %3d..%3d : %s' % (a, z, 'WILD' if v else 'clean'))
