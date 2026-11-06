import numpy as np
from collections import Counter

W, H = 320, 240
data = open("raw_frame.bin", "rb").read()
arr = np.frombuffer(data, dtype=">u2").reshape(H, W).astype(np.int32)

def spikes(ch, thr):
    p = np.pad(ch.astype(np.int32), 1, mode='edge')
    nb = np.zeros(ch.shape, dtype=np.float64)
    for dy in (0,1,2):
        for dx in (0,1,2):
            if dy==1 and dx==1: continue
            nb += p[dy:dy+ch.shape[0], dx:dx+ch.shape[1]]
    nb /= 8.0
    d = np.abs(ch.astype(np.int32) - nb)
    return d

r5 = (arr >> 11) & 0x1F
g6 = (arr >> 5) & 0x3F
b5 = arr & 0x1F

for name, ch in [("R5", r5), ("G6", g6), ("B5", b5)]:
    d = spikes(ch, 0)
    mask = d > 6
    ys, xs = np.where(mask)
    print(f"--- {name}: {len(ys)} spikes (>6) ---")
    if len(ys) == 0:
        continue
    print("   x mod 16 hist:", sorted(Counter(xs % 16).items()))
    print("   y mod  6 hist:", sorted(Counter(ys % 6).items()))
    print("   y mod 16 hist:", sorted(Counter(ys % 16).items()))
    print("   x mod  2 hist:", sorted(Counter(xs % 2).items()))
    print("   deviation hist:", sorted(Counter(d[mask].astype(int)).items())[:12])
    print("   row hist (y):", sorted(Counter(ys).items())[:12], "...")
    print()

print("=== per-BIT analysis: which bit positions are corrupted? ===")
for bit in range(16):
    b = (arr >> bit) & 1
    p = np.pad(b, 1, mode='edge')
    nb = np.zeros(b.shape, dtype=np.float64)
    for dy in (0,1,2):
        for dx in (0,1,2):
            if dy==1 and dx==1: continue
            nb += p[dy:dy+b.shape[0], dx:dx+b.shape[1]]
    nb /= 8.0
    d = np.abs(b - nb)
    flips = (d > 0.4).sum()
    print(f"  bit {bit:2d} (val {1<<bit:5d}): flips={flips:6d} ({100*flips/b.size:.3f}%)  meanflip={d.mean():.4f}")
