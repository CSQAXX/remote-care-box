import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    if len(d) >= 153600:
        return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)
    raise ValueError('short %s %d' % (fn, len(d)))


def ac1(P):
    p = P.astype(float).ravel()
    return float(np.corrcoef(p[:-1], p[1:])[0, 1])


def sm(P):
    C = P[1:-1, 1:-1]
    nb = np.stack([P[1:-1, :-2], P[1:-1, 2:], P[:-2, 1:-1], P[2:, 1:-1]])
    return float(np.abs(C - np.median(nb, axis=0)).mean())


def med_resid(P):
    m = np.median(np.stack([P[:, :-2], P[:, 2:]]), axis=0)
    out = np.zeros_like(P)
    out[:, 1:-1] = np.abs(P[:, 1:-1].astype(np.int64) - m.astype(np.int64))
    return out


def analyze(fn):
    f = load(fn)
    A = f[:, :, 0].astype(np.int64)
    B = f[:, :, 1].astype(np.int64)
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    r = med_resid(G2)[:, 1:-1]
    print('== %s' % fn)
    print('   R5 ac1 %.3f  G3 ac1 %.3f  G2 ac1 %.3f  B5 ac1 %.3f' % (
        ac1(R5), ac1(G3), ac1(G2), ac1(B5)))
    print('   smoothness  R5 %.2f  G3 %.2f  G2 %.2f  B5 %.2f' % (
        sm(R5), sm(G3), sm(G2), sm(B5)))
    print('   G2 bad(resid>3) %6.2f%%   mean resid %.3f' % ((r > 3).mean() * 100, r.mean()))
    h = np.bincount(G2.ravel(), minlength=8)
    print('   G2 hist %s' % h[:8].tolist())
    # render
    rgb = np.dstack([((R5) << 3).astype(np.uint8),
                     (((G3 << 3) | G2)).astype(np.uint8),
                     ((B5) << 3).astype(np.uint8)])
    Image.fromarray(rgb).resize((640, 480), Image.NEAREST).save(OUT + '\\%s.png' % fn[:-4])
    return rgb


for fn in ('cap_conv1.bin', 'cap_conv0.bin'):
    analyze(fn)
