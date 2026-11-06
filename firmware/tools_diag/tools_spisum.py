import numpy as np
import os, re, glob

W, H = 320, 240

def load(p):
    d = open(p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

tags = [re.match(r"u_(.+)_a\.bin", f).group(1) for f in glob.glob("u_*_a.bin")]
tags = [t for t in ["spi60","spi40","spi20","spi10"] if t in tags]

print(f"{'SPI时钟':>10s} {'帧间差异%':>10s} {'bit翻转率%':>11s} {'差>4占比%':>11s} {'差>100占比%':>12s}")
print("-" * 60)
for t in tags:
    a = load(f"u_{t}_a.bin"); b = load(f"u_{t}_b.bin")
    if a is None or b is None:
        print(f"{t:>10s}  (bad)")
        continue
    d = (a != b)
    xor = (a ^ b)
    flips = sum(((xor >> bit) & 1).sum() for bit in range(16))
    ad = np.abs(a - b)
    print(f"{t:>10s} {100*d.sum()/(W*H):9.2f}% {100*flips/16/(W*H):10.2f}% "
          f"{100*(ad>4).sum()/(W*H):10.2f}% {100*(ad>100).sum()/(W*H):11.2f}%")
