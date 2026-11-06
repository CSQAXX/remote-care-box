import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn, n=153600):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:n], dtype=np.uint8)


def test_shift(fn):
    """
    HYPOTHESIS: the byte stream is misaligned by ONE BYTE.
    If so, shifting the whole stream by 1 byte should turn the
    noisy "low byte" into a clean "low byte".
    """
    b = load(fn)
    print('== %s ==' % fn)
    for sh in (0, 1, -1, 2):
        s = np.roll(b, sh)
        hi = s[0::2].astype(np.int64)
        lo = s[1::2].astype(np.int64)
        dh = np.abs(np.diff(hi)).mean()
        dl = np.abs(np.diff(lo)).mean()
        # vertical correlation of each stream (row-to-row continuity)
        hi2 = hi.reshape(240, 320); lo2 = lo.reshape(240, 320)
        vh = np.corrcoef(hi2[:-1].ravel().astype(float), hi2[1:].ravel().astype(float))[0, 1]
        vl = np.corrcoef(lo2[:-1].ravel().astype(float), lo2[1:].ravel().astype(float))[0, 1]
        print('   shift %+d: hi adj %6.2f vert %.3f | lo adj %6.2f vert %.3f  %s' % (
            sh, dh, vh, dl, vl, '<== BALANCED/CLEAN' if abs(dh - dl) < 8 else ''))
    print()


def corr_odd_even(b):
    """Is the even-byte stream and odd-byte stream in phase?"""
    hi = b[0::2].astype(np.float64); lo = b[1::2].astype(np.float64)
    return np.corrcoef(hi, lo)[0, 1]


print('=== HALF-BYTE-ALIGNMENT HYPOTHESIS ===')
print('If the DVP data landed with a 1-byte offset relative to the')
print('pixel grid, one of the two byte streams would be "clean" and')
print('the other "noisy" -- exactly what we see.')
print()
for fn in ('raw3_1.bin', 'h_http_0.bin', 'h_conv0_0.bin'):
    test_shift(fn)

print('=== Even/odd byte stream correlation (in-phase check) ===')
for fn in ('raw3_1.bin', 'h_http_0.bin', 'h_conv0_0.bin'):
    b = load(fn)
    print('  %-16s corr(even,odd) = %+.3f' % (fn, corr_odd_even(b)))

print()
print('=== If we assume the buffer is really YUYV (2 bytes/px, 8-bit bus) ===')
print('i.e. byte order Y0 U0 Y1 V1 -- then even=YES luma, odd=chroma')
print('Test: chroma (odd) should be 2x-subsampled => smoother per-pixel-pair')
for fn in ('raw3_1.bin', 'h_http_0.bin'):
    b = load(fn)
    hi = b[0::2].astype(np.int64).reshape(240, 320)
    lo = b[1::2].astype(np.int64).reshape(240, 320)
    # odd bytes as U0,V0,U1,V1 -> reshape pairs
    U = lo[:, 0::2]; V = lo[:, 1::2]
    print('  %-16s  luma |x-x+1| %6.2f | chroma(U) |k-k+1| %6.2f | chroma(V) %6.2f' % (
        fn, np.abs(np.diff(hi, axis=1)).mean(),
        np.abs(np.diff(U, axis=1)).mean(), np.abs(np.diff(V, axis=1)).mean()))
