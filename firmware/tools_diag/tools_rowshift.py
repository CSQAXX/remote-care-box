import numpy as np
from PIL import Image

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

a = load("cbhttp0.bin")

print("=== 前 8 行的原始像素 (第 0-11 列) ===")
for y in range(8):
    print(f"  行{y}: {a[y, 0:12].tolist()}")

print()
print("=== 前 8 行, 每行有多少个 0xFFFF / 0x0000 ===")
for y in range(8):
    row = a[y]
    nF = (row == 0xFFFF).sum()
    n0 = (row == 0x0000).sum()
    other = W - nF - n0
    print(f"  行{y}: FFFF={nF:3d} 0000={n0:3d} 其他={other:3d}")

print()
print("=== 彩条理论图案 (GC0308 横条彩条) ===")
print("  8 条: 黑/白/黄/青/绿/品红/红/蓝, 每条约 30 行")
print()

print("=== 检查: 行 0-127 是否是 1 像素周期的黑白交替 (典型的 DVP 行错位) ===")
alt = 0
for y in range(0, 127):
    row = a[y]
    dom = row[np.argmax(np.unique(row, return_counts=True)[0])]
    nxt = a[y+1]
    dom2 = nxt[np.argmax(np.unique(nxt, return_counts=True)[0])]
    if dom != dom2:
        alt += 1
print(f"  行0-127 中相邻行主色不同的有 {alt}/127 行")

print()
print("=== 每 4 行分块看第 100 列 ===")
for y in range(0, 136, 4):
    print(f"  行{y:3d}: x100=0x{a[y,100]:04X}  x101=0x{a[y,101]:04X}  x150=0x{a[y,150]:04X}")
