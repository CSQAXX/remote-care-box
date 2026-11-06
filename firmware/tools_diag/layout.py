import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    return np.frombuffer(open(OUT + '\\' + fn, 'rb').read()[:153600], dtype=np.uint8)


def c(x, y):
    x = x.astype(float).ravel(); y = y.astype(float).ravel()
    x = x - x.mean(); y = y - y.mean()
    d = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / d) if d > 1e-9 else 0.0


def lag1(P):
    return c(P[:, :-1], P[:, 1:])


def interpret(b, name):
    """return (R,G,B) planes for a candidate interpretation"""
    if name == 'be':          # big endian: hi=even, lo=odd
        A = b[0::2].reshape(240, 320).astype(np.int64)
        B = b[1::2].reshape(240, 320).astype(np.int64)
    elif name == 'le':        # little endian: hi=odd, lo=even
        A = b[1::2].reshape(240, 320).astype(np.int64)
        B = b[0::2].reshape(240, 320).astype(np.int64)
    elif name == 'shift1':    # start at byte 1
        s = b[1:1 + 153600]
        A = s[0::2].reshape(240, 320).astype(np.int64)
        B = s[1::2].reshape(240, 320).astype(np.int64)
    elif name == 'shift2':
        s = b[2:2 + 153600]
        A = s[0::2].reshape(240, 320).astype(np.int64)
        B = s[1::2].reshape(240, 320).astype(np.int64)
    else:
        return None

    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    R = (R5 * 8).astype(float)
    G = ((G3 << 3) | G2).astype(float)
    Bp = (B5 * 8).astype(float)
    return R, G, Bp


print('=== MODE 0 (RGB565): which byte interpretation gives a NATURAL image? ===')
print('   criterion: corr(R,G) and corr(R,B) high  AND  within-channel lag1 > lag2')
for i in range(4):
    b = load('m0_%d.bin' % i)
    print('m0_%d' % i)
    for name in ('be', 'le', 'shift1', 'shift2'):
        R, G, Bp = interpret(b, name)
        rg = c(R, G); rb = c(R, Bp); gb = c(G, Bp)
        l1R = lag1(R); l2R = c(R[:, :-2], R[:, 2:])
        l1G = lag1(G)
        print('   %-7s  corrRG=%+.3f corrRB=%+.3f corrGB=%+.3f | lag1 R%.3f G%.3f  lag2 R%.3f | sum=%.3f'
              % (name, rg, rb, gb, l1R, l1G, l2R, rg + rb + gb))

print()
print('=== MODE 3 (raw YUV422): test Y/Cb/Cr ordering & 2-pixel swap ===')
for fn in ('truth_0.bin', 'http_truth_0.bin', 'truth_2.bin'):
    b = load(fn).reshape(240, 320, 2)
    print(fn)
    Y = b[:, :, 0].astype(float)
    Cb = b[:, 0::2, 1].astype(float)   # chroma at even column
    Cr = b[:, 1::2, 1].astype(float)   # chroma at odd column
    print('   corr(Y,Cb)=%.3f corr(Y,Cr)=%.3f corr(Cb,Cr)=%.3f' % (c(Y, Cb), c(Y, Cr), c(Cb, Cr)))
    print('   lag1: Y=%.3f Cb=%.3f Cr=%.3f' % (lag1(Y), lag1(Cb), lag1(Cr)))
    # try: swap the two chroma samples (Cb/Cr reversed)
    print('   if Cb/Cr swapped: corr(Y,Crsw)=%.3f' % c(Y, Cr))
