import numpy as np
import os, glob, re

W, H = 320, 240

def load(p):
    d = open(p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

groups = {}
for f in glob.glob("w_*_*.bin"):
    m = re.match(r"w_(.+)_(\d+)\.bin", f)
    groups.setdefault(m.group(1), []).append((int(m.group(2)), f))

print(f"{'配置':10s} {'帧间差异%':>10s} {'bit翻转率%':>11s} {'差>4占比%':>11s} {'差>100占比%':>12s}")
print("-" * 60)
for tag in ["worst", "best", "worst2", "best2"]:
    if tag not in groups:
        continue
    items = sorted(groups[tag])
    diffs, flips, big, huge = [], [], [], []
    for i in range(len(items) - 1):
        a = load(items[i][1]); b = load(items[i+1][1])
        if a is None or b is None:
            continue
        diffs.append(100 * (a != b).sum() / (W*H))
        xor = (a ^ b)
        flips.append(100 * sum(((xor >> k) & 1).sum() for k in range(16)) / 16 / (W*H))
        ad = np.abs(a - b)
        big.append(100 * (ad > 4).sum() / (W*H))
        huge.append(100 * (ad > 100).sum() / (W*H))
    if not diffs:
        continue
    print(f"{tag:10s} {np.mean(diffs):9.2f}% {np.mean(flips):10.2f}% "
          f"{np.mean(big):10.2f}% {np.mean(huge):11.2f}%   (n={len(diffs)} 对)")
