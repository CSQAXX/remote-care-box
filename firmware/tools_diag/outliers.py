import numpy as np, glob
from PIL import Image

def load(pat):
    out = []
    for f in sorted(glob.glob(pat)):
        d = open(f, 'rb').read()
        i = d.find(b'\r\n\r\n')
        body = d[i + 4:]
        if len(body) < 153600:
            continue
        out.append(np.frombuffer(body[:153600], dtype=np.uint8).reshape(240, 320, 2))
    return out

fr = load(r'D:\Software\kanhu\remote-care-box\raw3_*.bin')
f0, f1 = fr[0], fr[1]

w0 = f0.astype(np.int64); w1 = f1.astype(np.int64)
d = np.abs(w0 - w1)                     # per-byte change
dpix = d.max(axis=2)                    # per-pixel max byte change

print("=== OUTLIER ANALYSIS: is corruption sparse (bit errors) or dense? ===")
for t in (8, 16, 32, 64, 128):
    frac = (dpix > t).mean()
    print("  pixels with |delta| > %3d : %6.3f%%  (%d of %d)" % (t, frac*100, (dpix>t).sum(), dpix.size))

print("\n=== spatial distribution of big changes ===")
big = (dpix > 64)
print("  rows containing a big change: %d of 240" % big.any(axis=1).sum())
print("  cols containing a big change: %d of 320" % big.any(axis=0).sum())
print("  per-row counts of big changes [::20]:", big.sum(axis=1)[::20].tolist())
print("  per-col counts of big changes [::20]:", big.sum(axis=0)[::20].tolist())

# run lengths of consecutive big pixels
print("\n=== run-length structure (are big changes isolated pixels or runs?) ===")
row = big[120]
runs = []
c = 0
for v in row:
    if v: c += 1
    elif c: runs.append(c); c = 0
if c: runs.append(c)
from collections import Counter
print("  row120 run lengths:", dict(Counter(runs)))
allruns = []
for r in range(240):
    c = 0
    for v in big[r]:
        if v: c += 1
        elif c: allruns.append(c); c = 0
    if c: allruns.append(c)
print("  whole-frame run length histogram:", dict(Counter(allruns)))

print("\n=== are the SAME pixels corrupted each frame? (fixed defect vs random) ===")
# need a third frame for this; use frame pair disagreement pattern
print("  (need 3+ frames)")

print("\n=== does the change look like bit flips? ===")
x = w0 ^ w1
bits = np.unpackbits(x.astype(np.uint8))
print("  mean bits flipped per changed byte: %.2f" % (bits.sum(axis=0)[bits.sum(axis=0)>0].mean() if (bits.sum(axis=0)>0).any() else 0))
print("  per-bit-position flip rate:")
for bit in range(16):
    col = bits[:, :, bit].ravel()
    print("    bit %2d: flip rate %.4f" % (bit, col.mean()))
