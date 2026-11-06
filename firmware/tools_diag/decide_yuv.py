import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn, n=153600):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:n], dtype=np.uint8)


def show(fn):
    b = load(fn)
    even = b[0::2].astype(np.int64).reshape(240, 320)
    odd = b[1::2].astype(np.int64).reshape(240, 320)
    hi2 = b[1::2].astype(np.int64).reshape(240, 320)   # shifted interpretation
    print('== %s ==' % fn)

    # --- Interpretation 1: RGB565 big-endian ---
    A = even; B = odd
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    rgb565 = np.dstack([((R5) << 3).astype(np.uint8),
                        (((G3 << 3) | G2)).astype(np.uint8),
                        ((B5) << 3).astype(np.uint8)])

    # --- Interpretation 2: YUYV 8-bit (even=Y, odd=U/V) ---
    Y = even.astype(float)
    U = np.repeat(odd[:, 0::2].astype(float), 2, axis=1)
    V = np.repeat(odd[:, 1::2].astype(float), 2, axis=1)
    yuyv = np.clip(np.dstack([
        Y + 1.402 * (V - 128),
        Y - 0.344136 * (U - 128) - 0.714136 * (V - 128),
        Y + 1.772 * (U - 128)]), 0, 255).astype(np.uint8)

    # --- Interpretation 3: YUYV but shifted by one byte (odd=Y, even=U/V) ---
    Y2 = hi2.astype(float)
    U2 = np.repeat(even[:, 0::2].astype(float), 2, axis=1)
    V2 = np.repeat(even[:, 1::2].astype(float), 2, axis=1)
    yuyv_shift = np.clip(np.dstack([
        Y2 + 1.402 * (V2 - 128),
        Y2 - 0.344136 * (U2 - 128) - 0.714136 * (V2 - 128),
        Y2 + 1.772 * (U2 - 128)]), 0, 255).astype(np.uint8)

    for nm, img in (('rgb565', rgb565), ('yuyv', yuyv), ('yuyv_shift1', yuyv_shift)):
        g = img.mean(axis=2).astype(float)
        lap = g[1:-1,1:-1]*4 - g[:-2,1:-1] - g[2:,1:-1] - g[1:-1,:-2] - g[1:-1,2:]
        print('   %-12s lapstd %7.2f  adjdiff %6.2f  std %6.2f' % (
            nm, lap.std(), np.abs(np.diff(g, axis=1)).mean(), g.std()))
        Image.fromarray(img).resize((640, 480), Image.NEAREST).save(
            OUT + ('\\y_%s_%s.png' % (fn[:-4], nm)))

    # Chroma pair-share test: in real YUYV, U[2k]==U[2k+1]
    def pairshare(P):
        return float(np.abs(P[:, 0::2] - P[:, 1::2]).mean())
    print('   chroma pair-share |U[2k]-U[2k+1]| = %.2f  (small => real YUV422)' % pairshare(odd))
    print()


for fn in ('raw3_1.bin', 'h_http_0.bin', 'h_conv0_0.bin'):
    show(fn)
