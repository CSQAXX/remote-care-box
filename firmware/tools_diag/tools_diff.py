import numpy as np

W, H = 320, 240
a = np.frombuffer(open("cb_b.bin","rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)
b = np.frombuffer(open("cb_c.bin","rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

diff = (a != b)
print(f"pixels differing between two consecutive colorbar frames: {diff.sum()} / {W*H} = {100*diff.sum()/(W*H):.2f}%")
if diff.any():
    d = np.abs(a - b)
    print(f"  max abs diff = {d.max()}")
    print(f"  mean abs diff (where differ) = {d[diff].mean():.2f}")
    print("  bit flip counts:")
    for bit in range(16):
        flips = (((a ^ b) >> bit) & 1).sum()
        print(f"    bit {bit:2d}: {flips:6d} ({100*flips/(W*H):.3f}%)")

print()
print("=== per-ROW: are colorbar rows flat? ===")
r5 = (a >> 11) & 0x1F
g6 = (a >> 5) & 0x3F
b5 = a & 0x1F
for y in range(0, H, 16):
    for nm, ch in (("R5", r5), ("G6", g6), ("B5", b5)):
        row = ch[y]
        u = np.unique(row)
        if len(u) > 1:
            pass
    vals, counts = np.unique(a[y], return_counts=True)
    print(f"  row {y:3d}: {len(vals):3d} distinct, top3={sorted(zip(counts,vals), reverse=True)[:3]}")
