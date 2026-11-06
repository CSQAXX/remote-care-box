import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8)


def yuv_to_rgb(f):
    f = f.reshape(240, 320, 2).astype(np.float64)
    Y = f[:, :, 0]
    U = np.repeat(f[:, 0::2, 1], 2, axis=1)
    V = np.repeat(f[:, 1::2, 1], 2, axis=1)
    return np.clip(np.dstack([
        Y + 1.402 * (V - 128),
        Y - 0.344136 * (U - 128) - 0.714136 * (V - 128),
        Y + 1.772 * (U - 128)]), 0, 255).astype(np.uint8)


def rgb565_to_rgb(f):
    f = f.reshape(240, 320, 2).astype(np.int64)
    A = f[:, :, 0]; B = f[:, :, 1]
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    G6 = (G3 << 3) | G2
    return np.dstack([((R5) << 3).astype(np.uint8),
                      G6.astype(np.uint8),
                      ((B5) << 3).astype(np.uint8)])


def hcorr(P):
    """horizontal lag-1 autocorrelation of a 2D plane (0..1). real image ~0.95+, noise ~0"""
    p = P.astype(float)
    a = p[:, :-1].ravel(); b = p[:, 1:].ravel()
    a = a - a.mean(); b = b - b.mean()
    return float((a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum()))


def vcorr(P):
    p = P.astype(float)
    a = p[:-1, :].ravel(); b = p[1:, :].ravel()
    a = a - a.mean(); b = b - b.mean()
    return float((a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum()))


def report(tag, rgb):
    R = rgb[:, :, 0].astype(float); G = rgb[:, :, 1].astype(float); B = rgb[:, :, 2].astype(float)
    # high-frequency: residual from 3x3 median-ish (just laplacian)
    def lap(P):
        return float(np.abs(P[1:-1,1:-1]*4 - P[:-2,1:-1] - P[2:,1:-1] - P[1:-1,:-2] - P[1:-1,2:]).mean())
    # row-to-row change of row means (banding detector)
    rowmean = rgb.reshape(240, -1).mean(axis=1)
    band = np.abs(np.diff(rowmean)).mean()
    print('  %-14s hcorr R%.3f G%.3f B%.3f | lap R%5.2f G%5.2f B%5.2f | rowmean-step %5.2f'
          % (tag, hcorr(R), hcorr(G), hcorr(B), lap(R), lap(G), lap(B), band))
    return hcorr(G)


print('=== TRUTH: raw YUV422 from sensor (mode 3), converted to RGB ===')
for i in range(3):
    fn = 'truth_%d.bin' % i
    try:
        f = load(fn)
    except Exception:
        continue
    rgb = yuv_to_rgb(f)
    report(fn, rgb)
    Image.fromarray(rgb).resize((640, 480), Image.NEAREST).save(OUT + '\\cmp_truth_%d.png' % i)

print()
print('=== MODE 0: YUV422 + hardware converter -> RGB565 (raw dump) ===')
import os
for fn in sorted(os.listdir(OUT)):
    if fn.startswith('m0_') and fn.endswith('.bin'):
        f = load(fn)
        rgb = rgb565_to_rgb(f)
        report(fn, rgb)
        Image.fromarray(rgb).resize((640, 480), Image.NEAREST).save(OUT + '\\cmp_%s.png' % fn[:-4])
