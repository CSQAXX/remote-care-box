import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    return np.frombuffer(open(OUT + '\\' + fn, 'rb').read()[:153600], dtype=np.uint8)


def autocorr_lags(P, maxlag=8):
    """normalised autocorrelation at lags 0..maxlag (horizontally)"""
    p = P.astype(float)
    p = p - p.mean()
    out = []
    for L in range(0, maxlag + 1):
        a = p[:, :320 - L].ravel()
        b = p[:, L:].ravel()
        d = np.sqrt((a * a).sum() * (b * b).sum())
        out.append(float((a * b).sum() / d) if d > 1e-9 else 0.0)
    return out


print('=== HORIZONTAL AUTOCORRELATION vs LAG (mode 3 raw YUV422) ===')
for fn in ('truth_0.bin', 'truth_1.bin', 'truth_2.bin',
           'http_truth_0.bin', 'http_truth_1.bin', 'http_truth_2.bin'):
    b = load(fn).reshape(240, 320, 2)
    Y = b[:, :, 0].astype(float)   # even bytes = luma
    C = b[:, :, 1].astype(float)   # odd bytes = chroma
    ay = autocorr_lags(Y)
    ac = autocorr_lags(C)
    print('%s' % fn)
    print('   Y  lag: ' + ' '.join('%d:%.4f' % (i, v) for i, v in enumerate(ay)))
    print('   C  lag: ' + ' '.join('%d:%.4f' % (i, v) for i, v in enumerate(ac)))

print()
print('=== mode 0 RGB565: autocorrelation per byte plane ===')
for i in range(4):
    b = load('m0_%d.bin' % i).reshape(240, 320, 2)
    A = b[:, :, 0].astype(float)   # hi byte
    B = b[:, :, 1].astype(float)   # lo byte
    print('m0_%d' % i)
    print('   hi lag: ' + ' '.join('%d:%.4f' % (k, v) for k, v in enumerate(autocorr_lags(A))))
    print('   lo lag: ' + ' '.join('%d:%.4f' % (k, v) for k, v in enumerate(autocorr_lags(B))))

print()
print('=== TEST: does 1-byte-offset realignment clean up the Y plane? ===')
for fn in ('truth_0.bin', 'truth_1.bin', 'http_truth_0.bin'):
    raw = load(fn)
    print('%s' % fn)
    for off in (0, 1):
        shifted = raw[off:]
        Y = shifted[:153600].reshape(240, 320, 2)[:, :, 0].astype(float)
        ay = autocorr_lags(Y)
        print('   offset=%d  Y lag1=%.4f lag2=%.4f lag3=%.4f lag4=%.4f'
              % (off, ay[1], ay[2], ay[3], ay[4]))
    # also: treat as raw pairs (even/odd) -> check period-2 alternation energy
    b = raw.reshape(240, 320, 2)
    E = b[:, :, 0].astype(float)
    O = b[:, :, 1].astype(float)
    print('   E mean %.1f  O mean %.1f  |E-O| mean %.1f' % (E.mean(), O.mean(), np.abs(E - O).mean()))
    # period-2: correlation of even-index columns vs odd-index columns
    print('   E column-mean spread: %.2f   O column-mean spread: %.2f'
          % (E.mean(axis=0).std(), O.mean(axis=0).std()))
