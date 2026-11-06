import numpy as np
from collections import Counter

W, H = 320, 240
data = open("raw_frame.bin", "rb").read()
arr = np.frombuffer(data, dtype=">u2").reshape(H, W).astype(np.int32)

r5 = (arr >> 11) & 0x1F
g6 = (arr >> 5) & 0x3F
b5 = arr & 0x1F

print("=== per-row: dominant value and how many pixels differ from it ===")
for y in range(0, H, 16):
    row = arr[y]
    vals, counts = np.unique(row, return_counts=True)
    dom = vals[np.argmax(counts)]
    diff = (row != dom).sum()
    print(f"  row {y:3d}: dominant=0x{dom:04X} count={counts.max():3d}/{W}  differing={diff:3d}  distinct={len(vals)}")

print("\n=== per-bit: fraction of pixels differing from ROW DOMINANT value ===")
for bit in range(16):
    b = (arr >> bit) & 1
    bad = 0
    for y in range(H):
        vals, counts = np.unique(b[y], return_counts=True)
        dom = vals[np.argmax(counts)]
        bad += (b[y] != dom).sum()
    print(f"  bit {bit:2d}: {bad:6d}/{W*H} = {100*bad/(W*H):.3f}%")

print("\n=== rows that are NOT perfectly uniform (all pixels equal) ===")
nonuni = []
for y in range(H):
    u = np.unique(arr[y])
    if len(u) != 1:
        nonuni.append((y, len(u), arr[y][:8].tolist()))
print(f"  {len(nonuni)} rows non-uniform out of {H}")
for y, n, head in nonuni[:15]:
    print(f"    row {y:3d}: {n} distinct, head={head}")
