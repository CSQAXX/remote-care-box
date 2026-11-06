import urllib.request, numpy as np, sys, os, time

BASE = 'http://192.168.2.138:8080'
OUT = r'D:\Software\kanhu\remote-care-box'


def raw():
    d = urllib.request.urlopen(BASE + '/raw?capture', timeout=15).read()
    if len(d) < 153600:
        raise ValueError('short %d' % len(d))
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


def fields(f):
    A = f[:, :, 0].astype(np.int64); B = f[:, :, 1].astype(np.int64)
    return {'R5': A >> 3, 'G3': A & 7, 'G2': B >> 5, 'B5': B & 31}


def med_resid(P):
    m = np.median(np.stack([P[:, :-2], P[:, 2:]]), axis=0)
    out = np.zeros_like(P); out[:, 1:-1] = np.abs(P[:, 1:-1] - m)
    return out


def report(fs, tag):
    print('  %-4s %8s %8s %8s %8s %10s %8s' % ('frm', 'R5ac', 'G3ac', 'G2ac', 'B5ac', 'G2bad%', 'vertA'))
    for i, f in enumerate(fs):
        d = fields(f)
        acs = {k: float(np.corrcoef(v.ravel().astype(float)[:-1], v.ravel().astype(float)[1:])[0, 1]) for k, v in d.items()}
        r = med_resid(d['G2'])[:, 1:-1]
        A = f[:, :, 0].astype(float)
        vert = float(np.corrcoef(A[:-1].ravel(), A[1:].ravel())[0, 1])
        print('  %-4d %8.3f %8.3f %8.3f %8.3f %10.2f %8.3f' % (
            i, acs['R5'], acs['G3'], acs['G2'], acs['B5'], (r > 3).mean() * 100, vert))


if __name__ == '__main__':
    tag = sys.argv[1] if len(sys.argv) > 1 else 'http'
    fs = []
    for i in range(4):
        f = raw()
        fs.append(f)
        open(os.path.join(OUT, 'h_%s_%d.bin' % (tag, i)), 'wb').write(f.tobytes())
        time.sleep(0.2)
    print('== %s ==' % tag)
    report(fs, tag)
    print()
    print('  inter-frame G2 correlation:')
    for i in range(len(fs) - 1):
        a = fields(fs[i])['G2'].ravel().astype(float)
        b = fields(fs[i + 1])['G2'].ravel().astype(float)
        print('    f%d-f%d: %.3f' % (i, i + 1, np.corrcoef(a, b)[0, 1]))
