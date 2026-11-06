import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


def vert_corr(P):
    """correlation between row y and row y+1 (detects row shifts / dropped bytes)"""
    a = P[:-1].astype(float).ravel()
    b = P[1:].astype(float).ravel()
    return float(np.corrcoef(a, b)[0, 1])


def horiz_corr(P):
    a = P[:, :-1].astype(float).ravel()
    b = P[:, 1:].astype(float).ravel()
    return float(np.corrcoef(a, b)[0, 1])


files = ['raw3_1.bin', 'raw3_2.bin', 'raw_0.bin',
         'cap_conv1.bin', 'cap_conv0.bin',
         'seq_conv1_0.bin', 'seq_conv1_1.bin', 'seq_conv0_0.bin']

print('=== INTEGRITY TEST: is the byte stream intact? ===')
print('A real image: vertical corr (row y vs y+1) is high.')
print('If bytes were DROPPED in transit, rows shift -> vertical corr collapses.')
print()
print('%-20s %10s %10s %10s %10s' % ('file', 'vertA', 'vertB', 'horizA', 'horizB'))
for fn in files:
    try:
        f = load(fn)
    except Exception as e:
        print('%-20s  %s' % (fn, e)); continue
    A = f[:, :, 0].astype(np.int64); B = f[:, :, 1].astype(np.int64)
    print('%-20s %10.3f %10.3f %10.3f %10.3f' % (
        fn, vert_corr(A), vert_corr(B), horiz_corr(A), horiz_corr(B)))

print()
print('=== Per-channel vertical correlation (row continuity) ===')
for fn in files:
    try:
        f = load(fn)
    except Exception:
        continue
    A = f[:, :, 0].astype(np.int64); B = f[:, :, 1].astype(np.int64)
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    print('%-20s  R5 %.3f  G3 %.3f  G2 %.3f  B5 %.3f' % (
        fn, vert_corr(R5), vert_corr(G3), vert_corr(G2), vert_corr(B5)))
