import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    return np.frombuffer(open(OUT + '\\' + fn, 'rb').read()[:153600], dtype=np.uint8)


def c(x, y):
    x = np.asarray(x, dtype=float).ravel(); y = np.asarray(y, dtype=float).ravel()
    x = x - x.mean(); y = y - y.mean()
    d = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / d) if d > 1e-9 else 0.0


def aclag(x, maxlag=10):
    x = np.asarray(x, dtype=float).ravel(); x = x - x.mean()
    out = []
    for L in range(maxlag + 1):
        a = x[:len(x) - L]; b = x[L:]
        d = np.sqrt((a * a).sum() * (b * b).sum())
        out.append(float((a * b).sum() / d) if d > 1e-9 else 0.0)
    return out


def bitrev(u):
    r = np.zeros_like(u)
    for i in range(8):
        r |= ((u >> i) & 1) << (7 - i)
    return r


files = ['m0_0.bin', 'm0_1.bin', 'm0_2.bin', 'm0_3.bin',
         'truth_0.bin', 'http_truth_0.bin', 'http_truth_2.bin']

print('=== H1: is byte[2k+1] the complement of byte[2k]? ===')
for fn in files:
    b = load(fn)
    e = b[0::2].astype(np.int64); o = b[1::2].astype(np.int64)
    print('%-18s corr(E,O)=%+.4f  corr(E,255-O)=%+.4f  mean(E-O)=%.1f  mean(E+O)=%.1f'
          % (fn, c(e, o), c(e, 255 - o), (e - o).mean(), (e + o).mean()))

print()
print('=== H2: bit-reversed bytes ===')
for fn in files:
    b = load(fn)
    rb = bitrev(b)
    print('%-18s orig lag1=%.3f lag2=%.3f | bitrev lag1=%.3f lag2=%.3f lag4=%.3f'
          % (fn, aclag(b)[1], aclag(b)[2], aclag(rb)[1], aclag(rb)[2], aclag(rb)[4]))

print()
print('=== H3: stride interpretations (8-bit gray) ===')
for fn in files:
    b = load(fn)
    print('%-18s' % fn)
    for nm, d in (('even', b[0::2]), ('odd', b[1::2])):
        g = d[:76800].reshape(240, 320)
        a = aclag(g)
        print('    %-5s gray240x320 lag1=%+.3f lag2=%+.3f lag3=%+.3f lag4=%+.3f'
              % (nm, a[1], a[2], a[3], a[4]))
    g = b.reshape(240, 640)
    a = aclag(g)
    print('    640w  gray240x640 lag1=%+.3f lag2=%+.3f lag3=%+.3f lag4=%+.3f'
          % (a[1], a[2], a[3], a[4]))

print()
print('=== H4: 320-wide as GRAYSCALE 1 byte/px using stride-2 pairs? ===')
print('    (if the real image is 1 byte/px at 320 wide, then b[0::2] IS the image)')
for fn in files:
    b = load(fn)
    g = b[0::2][:76800].reshape(240, 320)
    Image.fromarray(g).resize((640, 480), Image.NEAREST).save(OUT + '\\stride_%s_even.png' % fn[:-4])
    g2 = b[1::2][:76800].reshape(240, 320)
    Image.fromarray(g2).resize((640, 480), Image.NEAREST).save(OUT + '\\stride_%s_odd.png' % fn[:-4])
print('    saved stride_*.png')
