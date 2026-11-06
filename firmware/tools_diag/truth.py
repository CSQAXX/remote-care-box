import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8)


def analyze(fn):
    """fn contains 320x240 YUV422 interleaved: Y0 U0 Y1 V1 ..."""
    b = load(fn)
    f = b.reshape(240, 320, 2).astype(np.int64)
    Y = f[:, :, 0]
    U = f[:, 0::2, 1]      # U at even x
    V = f[:, 1::2, 1]      # V at odd x

    def adj(P):
        return float(np.abs(np.diff(P.astype(float), axis=1)).mean())

    def rowadj(P):
        return np.abs(np.diff(P.astype(float), axis=1)).mean(axis=1)

    # In REAL YUV422 from a camera:
    #  - luma Y varies per pixel (has detail)
    #  - chroma U,V are 2x-subsampled => |U[k]-U[k+1]| (2 real px apart)
    #    should be comparable to or smaller than luma |Y[x]-Y[x+1]|
    print('== %s ==' % fn)
    print('   Y  (luma)  adj %6.2f   rows>25: %3d/240' % (adj(Y), (rowadj(Y) > 25).sum()))
    print('   U  (chroma) adj %6.2f   rows>25: %3d/240' % (adj(U), (rowadj(U) > 25).sum()))
    print('   V  (chroma) adj %6.2f   rows>25: %3d/240' % (adj(V), (rowadj(V) > 25).sum()))
    # chroma pair-share: in YUV422, U and V alternate per pixel PAIR
    # so within a pair of pixels (x even = U, x odd = V) they are DIFFERENT components
    print('   |U[k]-V[k]| (same pair, different components) %6.2f' %
          float(np.abs(U - V).mean()))
    # render as YUV -> RGB
    Uf = np.repeat(U, 2, axis=1).astype(float)
    Vf = np.repeat(V, 2, axis=1).astype(float)
    Yf = Y.astype(float)
    rgb = np.clip(np.dstack([
        Yf + 1.402 * (Vf - 128),
        Yf - 0.344136 * (Uf - 128) - 0.714136 * (Vf - 128),
        Yf + 1.772 * (Uf - 128)]), 0, 255).astype(np.uint8)
    Image.fromarray(rgb).resize((640, 480), Image.NEAREST).save(OUT + '\\truth_%s.png' % fn[:-4])
    Image.fromarray(Y.astype(np.uint8)).resize((640, 480), Image.NEAREST).save(OUT + '\\truth_%s_Y.png' % fn[:-4])
    return rgb


import glob, os
for fn in sorted(glob.glob(OUT + '\\truth_*.bin')):
    analyze(os.path.basename(fn))
