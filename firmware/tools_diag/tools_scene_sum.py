import numpy as np
import os

W, H = 320, 240

def load(p):
    d = open(p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

def report(a, b, label):
    if a is None or b is None:
        print(f"  {label}: bad")
        return
    diff = (a != b)
    pct = 100 * diff.sum() / (W * H)
    # 只统计"小差异"(1~4 步)的像素 —— 真实场景的曝光/噪声会有小抖动,
    # 大幅跳变才是数据被破坏。
    d = np.abs(a - b)
    small = ((d > 0) & (d <= 4)).sum()
    big = (d > 4).sum()
    huge = (d > 100).sum()
    print(f"  {label}: 差异 {pct:.2f}%  |差|<=4: {small} ({100*small/(W*H):.2f}%)  "
          f"差>4: {big} ({100*big/(W*H):.2f}%)  差>100: {huge} ({100*huge/(W*H):.2f}%)")

print("=== 真实场景相邻帧 ===")
f = [load(f"sc{i}.bin") for i in (1, 2, 3)]
report(f[0], f[1], "sc1 vs sc2")
report(f[1], f[2], "sc2 vs sc3")

print()
print("=== 彩条 (静止图样) 相邻帧 ===")
g = [load(f"cs{i}.bin") for i in (1, 2, 3)]
report(g[0], g[1], "cs1 vs cs2")
report(g[1], g[2], "cs2 vs cs3")
