import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


f = load('now_1.bin')
A = f[:, :, 0].astype(np.float64)
B = f[:, :, 1].astype(np.float64)

print('=== ROW DUPLICATION ANALYSIS ===')
print('row : corr(row, row-1)  maxdiff(row,row-1)')
for y in range(0, 40):
    c = np.corrcoef(A[y], A[y - 1])[0, 1] if y > 0 else 1.0
    d = np.abs(A[y] - A[y - 1]).mean() if y > 0 else 0
    print('  %3d : %8.4f   %8.3f' % (y, c, d))

print()
print('=== count EXACT duplicate rows ===')
exact = 0
for y in range(1, 240):
    if np.array_equal(A[y], A[y - 1]):
        exact += 1
print('  exactly identical row pairs: %d / 239' % exact)

print()
print('=== per-pixel row-pair identity: how many PIXELS in row y equal row y-1? ===')
for y in (1, 2, 5, 10, 50, 100, 150, 200):
    same = (A[y] == A[y - 1]).mean() * 100
    print('  row %3d: %.1f%% of pixels identical to row above' % (y, same))

print()
print('=== IMAGE CONTENT CHECK: is it a real image? ===')
print('  row 100 first 40 hi-bytes:', ' '.join('%3d' % v for v in A[100, :40].astype(int)))
print('  row 101 first 40 hi-bytes:', ' '.join('%3d' % v for v in A[101, :40].astype(int)))
print('  row 102 first 40 hi-bytes:', ' '.join('%3d' % v for v in A[102, :40].astype(int)))
print()
print('  per-row std of hi byte (0 => row is constant/uniform):')
rs = A.std(axis=1)
print('   ', np.round(rs[:40], 1).tolist())
print('  overall mean row std: %.2f' % rs.mean())
