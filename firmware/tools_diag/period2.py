import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    return np.frombuffer(open(OUT + '\\' + fn, 'rb').read()[:153600], dtype=np.uint8)


def aclag(x, maxlag=12):
    x = x.astype(float).ravel()
    x = x - x.mean()
    out = []
    for L in range(0, maxlag + 1):
        a = x[:len(x) - L]; b = x[L:]
        d = np.sqrt((a * a).sum() * (b * b).sum())
        out.append(float((a * b).sum() / d) if d > 1e-9 else 0.0)
    return out


files = ['m0_0.bin', 'm0_1.bin', 'm0_2.bin', 'm0_3.bin',
         'truth_0.bin', 'truth_1.bin', 'truth_2.bin',
         'http_truth_0.bin', 'http_truth_1.bin', 'http_truth_2.bin']

print('=== RAW BYTE STREAM autocorrelation (lag = byte offset) ===')
for fn in files:
    b = load(fn)
    a = aclag(b)
    print('%-18s ' % fn + ' '.join('%d:%.3f' % (i, v) for i, v in enumerate(a[:9])))

print()
print('=== TEST A: are consecutive bytes equal (byte duplication)? ===')
for fn in files:
    b = load(fn)
    print('%-18s  b[0]==b[1] %.1f%%   b[2k]==b[2k+1] %.1f%%   b[2k]==b[2k+2] %.1f%%'
          % (fn, (b[0::2] == b[1::2]).mean() * 100,
             (b[0::2] == b[1::2]).mean() * 100,
             (b[0::2] == b[2::2]).mean() * 100))

print()
print('=== TEST B: split plane E vs O, check A/B alternation ===')
for fn in files:
    b = load(fn).reshape(240, 320, 2)
    E = b[:, :, 0].astype(float); O = b[:, :, 1].astype(float)
    # split each row into even/odd columns within the plane
    print('%-18s E: lag1=%.3f lag2=%.3f | O: lag1=%.3f lag2=%.3f | E==O %.1f%%'
          % (fn, aclag(E)[1], aclag(E)[2], aclag(O)[1], aclag(O)[2],
             (b[:, :, 0] == b[:, :, 1]).mean() * 100))

print()
print('=== TEST C: reinterpret buffer as 240x320 8-bit GRAYSCALE using every other byte ===')
for fn in files:
    b = load(fn)
    for stride_name, data in (('even bytes', b[0::2]), ('odd bytes', b[1::2])):
        g = data[:76800].reshape(240, 320).astype(float)
        a = aclag(g)
        print('%-18s %-11s gray lag1=%.3f lag2=%.3f lag3=%.3f' % (fn, stride_name, a[1], a[2], a[3]))

print()
print('=== TEST D: reinterpret as 240x640 8-bit (half-width image) ===')
for fn in files:
    b = load(fn)
    g = b.reshape(240, 640).astype(float)
    a = aclag(g)
    print('%-18s 640-wide lag1=%.3f lag2=%.3f lag3=%.3f lag4=%.3f'
          % (fn, a[1], a[2], a[3], a[4]))
