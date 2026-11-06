import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'

PERMS = {
    'identity': (0, 1, 2, 3, 4, 5, 6, 7),
    'top1': (0, 7, 4, 1, 2, 3, 5, 6),
    'top2': (5, 6, 7, 0, 2, 1, 3, 4),
    'top3': (3, 6, 7, 0, 2, 1, 5, 4),
    'top4': (1, 5, 4, 7, 2, 0, 3, 6),
}


def load(fn):
    return np.frombuffer(open(OUT + '\\' + fn, 'rb').read()[:153600], dtype=np.uint8)


def unpermute(b, perm):
    """perm[i] = received bit index that becomes true bit i"""
    T = np.zeros(b.size, dtype=np.int32)
    for i in range(8):
        T |= ((b.astype(np.int32) >> perm[i]) & 1) << i
    return T.astype(np.uint8)


def to_rgb(T, swap_bytes=False):
    f = T.reshape(240, 320, 2).astype(np.int64)
    if swap_bytes:
        A = f[:, :, 1]; B = f[:, :, 0]
    else:
        A = f[:, :, 0]; B = f[:, :, 1]
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    return np.dstack([((R5) << 3).astype(np.uint8),
                      ((G3 << 3) | G2).astype(np.uint8),
                      ((B5) << 3).astype(np.uint8)])


def naturalness(rgb):
    """higher = more like a natural image"""
    def c(x, y):
        x = x.astype(float).ravel(); y = y.astype(float).ravel()
        x = x - x.mean(); y = y - y.mean()
        d = np.sqrt((x * x).sum() * (y * y).sum())
        return float((x * y).sum() / d) if d > 1e-9 else 0.0
    R, G, B = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return c(R, G), c(R, B), c(G, B)


files = ['m0_2.bin', 'm0_3.bin', 'http_truth_2.bin']
for fn in files:
    b = load(fn)
    print('=== %s ===' % fn)
    for name, perm in PERMS.items():
        for swap in (False, True):
            T = unpermute(b, perm)
            rgb = to_rgb(T, swap)
            rg, rb, gb = naturalness(rgb)
            tag = '%s%s' % (name, '_swap' if swap else '')
            print('  %-14s crossCorr RG=%+.3f RB=%+.3f GB=%+.3f  sum=%+.3f'
                  % (tag, rg, rb, gb, rg + rb + gb))
            if name in ('identity', 'top1', 'top2', 'top4') and not swap:
                Image.fromarray(rgb).resize((640, 480), Image.NEAREST).save(
                    OUT + '\\perm_%s_%s.png' % (fn[:-4], tag))
    print()
