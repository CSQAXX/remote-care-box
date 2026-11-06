import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


def render(f):
    A = f[:, :, 0].astype(np.int64); B = f[:, :, 1].astype(np.int64)
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    G6 = (G3 << 3) | G2
    return np.dstack([((R5) << 3).astype(np.uint8),
                      (G6).astype(np.uint8),
                      ((B5) << 3).astype(np.uint8)])


def g2noise(f):
    """EXPLICIT measure of the green-low-3-bits noise."""
    A = f[:, :, 0].astype(np.int64); B = f[:, :, 1].astype(np.int64)
    G2 = (B >> 5)
    # high-frequency energy of G2 ALONE (0..7 range)
    p = G2.astype(float)
    lap = np.abs(p[1:-1,1:-1]*4 - p[:-2,1:-1] - p[2:,1:-1]
                  - p[1:-1,:-2] - p[1:-1,2:]).mean()
    return lap


def rowdup(f):
    """how many wild rows are near-duplicates of an adjacent row (DMA overwrite)"""
    A = f[:, :, 0].astype(np.float64)
    n = 0
    for y in range(1, 239):
        row = A[y]
        best = max(abs(np.corrcoef(row, A[z])[0, 1]) for z in (y - 1, y + 1))
        if best > 0.99:
            n += 1
    return n


files = ['raw3_1.bin', 'raw3_2.bin', 'raw_0.bin',
         'now_0.bin', 'now_1.bin', 'now_2.bin',
         'h_http_0.bin', 'h_conv0_0.bin']

print('%-16s %12s %14s %16s' % ('file', 'G2-lapHF', 'adjRowDup/240', 'verdict'))
for fn in files:
    try:
        f = load(fn)
    except Exception as e:
        print(fn, 'ERR'); continue
    lap = g2noise(f)
    dup = rowdup(f)
    verdict = 'CLEAN' if lap < 0.35 else ('NOISY' if lap < 1.0 else 'VERY NOISY')
    print('%-16s %12.3f %14d %16s' % (fn, lap, dup, verdict))

print()
print('=== SIDE BY SIDE ===')
old = render(load('raw3_1.bin'))
new = render(load('now_1.bin'))
sheet = Image.new('RGB', (640, 960))
sheet.paste(Image.fromarray(old).resize((640, 480), Image.NEAREST), (0, 0))
sheet.paste(Image.fromarray(new).resize((640, 480), Image.NEAREST), (0, 480))
sheet.save(OUT + '\\ab_old_new.png')
print('saved ab_old_new.png  (top=old broken, bottom=now)')
