import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    return np.frombuffer(open(OUT + '\\' + fn, 'rb').read()[:153600], dtype=np.uint8)


def yuv_to_rgb(b):
    f = b.reshape(240, 320, 2).astype(np.float64)
    Y = f[:, :, 0]
    U = np.repeat(f[:, 0::2, 1], 2, axis=1)
    V = np.repeat(f[:, 1::2, 1], 2, axis=1)
    return np.clip(np.dstack([
        Y + 1.402 * (V - 128),
        Y - 0.344136 * (U - 128) - 0.714136 * (V - 128),
        Y + 1.772 * (U - 128)]), 0, 255).astype(np.uint8)


def best_shift(P):
    """find the (dy,dx) shift that maximizes correlation -> detects misalignment"""
    P = P.astype(float)
    best = (-2, 0, 0)
    for dy in (-1, 0, 1):
        for dx in (-2, -1, 0, 1, 2):
            if dy == 0 and dx == 0:
                a = P[:, :-1].ravel(); b = P[:, 1:].ravel()
            else:
                a = P[max(0, dy):240 + min(0, dy), max(0, dx):320 + min(0, dx)].ravel()
                b = P[max(0, -dy):240 + min(0, -dy), max(0, -dx):320 + min(0, -dx)].ravel()
            if len(a) < 1000:
                continue
            a = a - a.mean(); b = b - b.mean()
            d = np.sqrt((a * a).sum() * (b * b).sum())
            if d < 1e-9:
                continue
            c = float((a * b).sum() / d)
            if c > best[0]:
                best = (c, dy, dx)
    return best


for i in range(3):
    for tag in ('truth_%d.bin' % i, 'http_truth_%d.bin' % i):
        b = load(tag)
        rgb = yuv_to_rgb(b)
        Y = b.reshape(240, 320, 2)[:, :, 0].astype(float)
        print('%s' % tag)
        for nm, P in (('Y', Y), ('R', rgb[:, :, 0]), ('G', rgb[:, :, 1]), ('B', rgb[:, :, 2])):
            c, dy, dx = best_shift(P)
            print('    %-2s best corr %.4f at shift dy=%+d dx=%+d' % (nm, c, dy, dx))

print()
print('=== per-line vertical alignment: does each line match line y-1 shifted by some dx? ===')
b = load('truth_1.bin')
Y = b.reshape(240, 320, 2)[:, :, 0].astype(float)
for y in (10, 30, 60, 90, 120, 150, 180, 210):
    row = Y[y]
    bestc = -2; bestdx = 0
    for dx in range(-4, 5):
        if dx == 0:
            a = row[:-4]; c2 = row[4:]
        elif dx > 0:
            a = row[:-4 - dx]; c2 = row[dx + 4:]
        else:
            a = row[-dx:-dx - 4]; c2 = row[4 - dx:]
        a = a - a.mean(); c2 = c2 - c2.mean()
        d = np.sqrt((a*a).sum() * (c2*c2).sum())
        cc = float((a*c2).sum()/d) if d > 1e-9 else -2
        if cc > bestc:
            bestc = cc; bestdx = dx
    print('  row %3d: best corr %.4f at dx=%+d' % (y, bestc, bestdx))
