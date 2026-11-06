import numpy as np
import os, glob, re

W, H = 320, 240

def load(p):
    d = open(p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

tags = []
for f in sorted(glob.glob("t_*_a.bin")):
    tag = re.match(r"t_(.+)_a\.bin", f).group(1)
    if os.path.exists(f"t_{tag}_b.bin"):
        tags.append(tag)

print(f"{'tag':14s} {'帧间差异%':>10s} {'列内不一致%':>12s} {'bit翻转率%':>11s} {'差>100%':>9s}")
print("-" * 62)
rows = []
for t in tags:
    a = load(f"t_{t}_a.bin")
    b = load(f"t_{t}_b.bin")
    if a is None or b is None:
        print(f"{t:14s} (bad)")
        continue
    d = (a != b)
    diffpct = 100 * d.sum() / (W * H)

    tot_bad = 0
    for x in range(W):
        col = a[:, x]
        vals, counts = np.unique(col, return_counts=True)
        tot_bad += (col != vals[np.argmax(counts)]).sum()
    colpct = 100 * tot_bad / (W * H)

    xor = (a ^ b)
    flips = sum(((xor >> bit) & 1).sum() for bit in range(16))
    flipavg = 100 * flips / 16 / (W * H)

    absd = np.abs(a - b)
    huge = 100 * (absd > 100).sum() / (W * H)

    rows.append((t, diffpct, colpct, flipavg, huge))

for t, dp, cp, fa, hu in sorted(rows, key=lambda r: r[3]):
    print(f"{t:14s} {dp:9.2f}% {cp:11.2f}% {fa:10.2f}% {hu:8.2f}%")
