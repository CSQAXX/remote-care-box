import urllib.request, numpy as np, os

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'

print('=== capture mode3 YUV422 via HTTP ===')
for i in range(3):
    d = urllib.request.urlopen(BASE + '/raw?capture', timeout=20).read()
    open(OUT + '\\http_truth_%d.bin' % i, 'wb').write(d[:153600])
    print('  got %d bytes' % min(len(d), 153600))

print()
print('=== compare HTTP vs SERIAL capture (byte level) ===')
for i in range(3):
    try:
        a = open(OUT + '\\http_truth_%d.bin' % i, 'rb').read()[:153600]
        b = open(OUT + '\\truth_%d.bin' % i, 'rb').read()[:153600]
    except Exception as e:
        print('  missing', e); continue
    A = np.frombuffer(a, dtype=np.uint8); B = np.frombuffer(b, dtype=np.uint8)
    diff = (A != B)
    print('  frame%d: bytes differing = %d / 153600 (%.2f%%)' % (i, diff.sum(), diff.sum() * 100.0 / 153600))
    if diff.sum():
        idx = np.nonzero(diff)[0]
        print('     first diff at byte %d (row %d, col %d)' % (idx[0], idx[0] // 640, (idx[0] % 640) // 2))
        print('     contiguous? diff byte range: %d..%d' % (idx[0], idx[-1]))
        # are diffs confined to a few bytes or spread?
        print('     diff rows: %d of 240' % len(set((idx // 640).tolist())))
