import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    return np.frombuffer(open(OUT + '\\' + fn, 'rb').read()[:153600], dtype=np.uint8)


def c(x, y):
    x = np.asarray(x, float).ravel(); y = np.asarray(y, float).ravel()
    x = x - x.mean(); y = y - y.mean()
    d = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / d) if d > 1e-9 else 0.0


def aclag(P, maxlag=6):
    P = np.asarray(P, float).ravel(); P = P - P.mean()
    out = []
    for L in range(maxlag + 1):
        a = P[:len(P) - L]; b = P[L:]
        d = np.sqrt((a * a).sum() * (b * b).sum())
        out.append(float((a * b).sum() / d) if d > 1e-9 else 0.0)
    return out


def as_rgb565(b):
    f = b.reshape(240, 320, 2).astype(np.int64)
    A = f[:, :, 0]; B = f[:, :, 1]
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    return np.dstack([((R5) << 3).astype(np.uint8),
                      ((G3 << 3) | G2).astype(np.uint8),
                      ((B5) << 3).astype(np.uint8)]).astype(float)


def as_yuv422(b):
    """YUYV: byte0=Y, byte1=U, byte2=Y, byte3=V"""
    f = b.reshape(240, 320, 2).astype(float)
    Y = f[:, :, 0]
    U = np.repeat(f[:, 0::2, 1], 2, axis=1)
    V = np.repeat(f[:, 1::2, 1], 2, axis=1)
    return np.clip(np.dstack([Y + 1.402 * (V - 128),
                               Y - 0.344136 * (U - 128) - 0.714136 * (V - 128),
                               Y + 1.772 * (U - 128)]), 0, 255)


def score(rgb, tag):
    R, G, B = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    ar = aclag(R); ag = aclag(G); ab = aclag(B)
    print('  %-12s crossCorr RG=%+.3f RB=%+.3f GB=%+.3f | R lag1..3 %+.3f/%+.3f/%+.3f  G %+.3f/%+.3f/%+.3f  B %+.3f/%+.3f/%+.3f'
          % (tag, c(R, G), c(R, B), c(G, B),
             ar[1], ar[2], ar[3], ag[1], ag[2], ag[3], ab[1], ab[2], ab[3]))
    # natural image test: all channels should have DECAYING autocorr (lag1>lag2>lag3)
    mono = (ar[1] > ar[2] > ar[3]) and (ag[1] > ag[2] > ag[3]) and (ab[1] > ab[2] > ab[3])
    # and cross-channel correlation should be strongly POSITIVE
    cross = (c(R, G) > 0.7) and (c(R, B) > 0.7) and (c(G, B) > 0.7)
    print('               -> monotone-autocorr: %s   strong-cross-corr: %s   => %s'
          % ('YES' if mono else 'NO', 'YES' if cross else 'NO',
             'NATURAL IMAGE' if (mono and cross) else 'NOT NATURAL'))
    return mono and cross


print('################ MODE 0 (supposedly RGB565 via hardware converter) ################')
for i in range(4):
    b = load('m0_%d.bin' % i)
    print('m0_%d' % i)
    score(as_rgb565(b), 'as RGB565')
    score(as_yuv422(b), 'as YUV422 ')
    r = as_yuv422(b).astype(np.uint8)
    Image.fromarray(r).resize((640, 480), Image.NEAREST).save(OUT + '\\m0_%d_asYUV.png' % i)

print()
print('################ MODE 3 (raw YUV422 from sensor) ################')
for i in range(3):
    b = load('http_truth_%d.bin' % i)
    print('http_truth_%d' % i)
    score(as_rgb565(b), 'as RGB565')
    score(as_yuv422(b), 'as YUV422 ')
