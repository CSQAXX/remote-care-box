import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn, n=153600):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:n], dtype=np.uint8).reshape(240, -1)


print('=== CHECK 1: is my SERIAL capture contaminated by interleaved log text? ===')
print('(ESP_LOGI writes to the same COM3 as my binary dump -> could corrupt it)')
for fn in ('seq_conv1_0.bin', 'seq_conv0_0.bin', 'gray_0.bin'):
    d = open(OUT + '\\' + fn, 'rb').read()
    txt = sum(1 for b in d[:76800] if 32 <= b < 127)
    print('  %-20s ASCII-printable bytes in payload: %5.1f%%' % (fn, txt / 76800 * 100))

print()
print('=== CHECK 2: HTTP capture vs SERIAL capture of the same mode ===')
print('HTTP has no interleaving risk, so HTTP is the trustworthy source.')
for fn in ('h_http_0.bin', 'h_conv0_0.bin'):
    b = load(fn)
    r = b.reshape(240, 640).astype(np.int64)
    hi = r[:, 0::2]; lo = r[:, 1::2]
    dh = np.abs(np.diff(hi, axis=1)).mean(axis=1)
    dl = np.abs(np.diff(lo, axis=1)).mean(axis=1)
    wild = (dh > 25) | (dl > 25)
    # band structure
    runs = []
    cur = bool(wild[0]); st = 0
    for j in range(1, 240):
        if bool(wild[j]) != cur:
            runs.append((cur, st, j - 1)); cur = bool(wild[j]); st = j
    runs.append((cur, st, 239))
    wr = ['%d-%d' % (a, z) for v, a, z in runs if v]
    print('  %-16s wild rows %3d/240  bands: %s' % (fn, wild.sum(), ','.join(wr)))

print()
print('=== CHECK 3: in the wild region, is the data a STALE copy of a previous frame? ===')
print('(that is the signature of DMA overwriting the buffer we hold)')
for fn in ('h_http_0.bin', 'h_conv0_0.bin'):
    b = load(fn).reshape(240, 320, 2).astype(np.int64)
    # compare rows in the wild region against the SAME row of the clean region
    # (i.e. is a wild row equal to some other row of the frame?)
    A = b[:, :, 0].astype(np.int64)
    wild_rows = []
    for y in range(240):
        row = A[y]
        d = np.abs(np.diff(row)).mean()
        if d > 25:
            wild_rows.append(y)
    if wild_rows:
        y = wild_rows[len(wild_rows) // 2]
        row = A[y]
        best = (-1, -1)
        for z in range(240):
            if z == y:
                continue
            c = np.corrcoef(row, A[z])[0, 1]
            if c > best[0]:
                best = (c, z)
        print('  %-16s wild row %d best match = row %d (corr %.3f)' % (fn, y, best[1], best[0]))

print()
print('=== CHECK 4: what do the wild rows look like? (render) ===')
for fn in ('h_http_0.bin',):
    b = load(fn).reshape(240, 320, 2).astype(np.int64)
    A = b[:, :, 0]; B = b[:, :, 1]
    rgb = np.dstack([((A >> 3) << 3).astype(np.uint8),
                     (((A & 7) << 3) | ((B >> 5) & 7)).astype(np.uint8),
                     ((B & 31) << 3).astype(np.uint8)])
    Image.fromarray(rgb).resize((640, 480), Image.NEAREST).save(OUT + '\\http_frame.png')
    Image.fromarray(A.astype(np.uint8)).resize((640, 480), Image.NEAREST).save(OUT + '\\http_hi.png')
    Image.fromarray(B.astype(np.uint8)).resize((640, 480), Image.NEAREST).save(OUT + '\\http_lo.png')
    print('  saved http_frame.png / http_hi.png / http_lo.png')
