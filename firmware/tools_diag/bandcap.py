import numpy as np
import urllib.request, time, os, sys

BASE = 'http://192.168.2.138:8080'
OUT = r'D:\Software\kanhu\remote-care-box'


def raw():
    d = urllib.request.urlopen(BASE + '/raw?capture', timeout=15).read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


def band_analysis(f):
    """f: (240,320,2) array -> per-row adjacent-diff energy of hi/lo streams"""
    r = f.reshape(240, 640).astype(np.int64)
    hi = r[:, 0::2]; lo = r[:, 1::2]
    dh = np.abs(np.diff(hi, axis=1)).mean(axis=1)
    dl = np.abs(np.diff(lo, axis=1)).mean(axis=1)
    wild = ((dh > 25) | (dl > 25))
    return dh, dl, wild


def report(tag, n=3):
    fs = []
    for i in range(n):
        f = raw()
        fs.append(f)
        open(os.path.join(OUT, 'p_%s_%d.bin' % (tag, i)), 'wb').write(f.tobytes())
        time.sleep(0.15)
    print('=== %s ===' % tag)
    for i, f in enumerate(fs):
        dh, dl, wild = band_analysis(f)
        # contiguous wild runs
        runs = []
        cur = bool(wild[0]); st = 0
        for j in range(1, 240):
            if bool(wild[j]) != cur:
                runs.append((cur, st, j - 1)); cur = bool(wild[j]); st = j
        runs.append((cur, st, 239))
        wr = [f'{a}-{z}' for v, a, z in runs if v]
        print('  frame %d: wild rows %3d/240  runs: %s' % (i, wild.sum(), ','.join(wr)))
    # cross-frame stability of the wild region
    A = fs[0].reshape(240, 320, 2).astype(np.int64)
    B = fs[1].reshape(240, 320, 2).astype(np.int64)
    print('  frame0 vs frame1 identical bytes: %.1f%%' % ((A == B).all(axis=2).mean() * 100))
    for lo_r, hi_r in ((0, 118), (118, 240)):
        a = A[lo_r:hi_r].ravel(); b = B[lo_r:hi_r].ravel()
        print('    rows %3d-%3d: byte-identical %.1f%%   corr %.3f' % (
            lo_r, hi_r, (a == b).mean() * 100,
            np.corrcoef(a.astype(float), b.astype(float))[0, 1]))
    print()


if __name__ == '__main__':
    report(sys.argv[1] if len(sys.argv) > 1 else 'x')
