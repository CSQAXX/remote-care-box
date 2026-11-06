import numpy as np

W, H = 320, 240

def load(p):
    d = open(p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

a = load("fin_cb1.bin"); b = load("fin_cb2.bin")
d = np.abs(a - b)

print("=== 残差噪声的空间分布 (哪些行在跳) ===")
rows = []
for y in range(H):
    n = (d[y] > 4).sum()
    rows.append(n)
rows = np.array(rows)
print(f"  有噪声的行数: {(rows>0).sum()}/{H}")
print(f"  每行噪声像素数: 最大 {rows.max()}, 平均 {rows.mean():.1f}")
# 连续噪声行段
segs = []
inseg = False
for y in range(H):
    if rows[y] > 0 and not inseg:
        start = y; inseg = True
    elif rows[y] == 0 and inseg:
        segs.append((start, y-1)); inseg = False
if inseg: segs.append((start, H-1))
print(f"  噪声行段: {segs}")

print()
print("=== 噪声行的 x 分布 ===")
for y in range(H):
    if rows[y] > 0:
        xs = np.where(d[y] > 4)[0]
        print(f"  row {y:3d}: {len(xs)} 个坏像素, x范围 [{xs.min()},{xs.max()}], "
              f"连续段数≈{1+np.sum(np.diff(xs)>1)}")
        if y > 20:
            break

print()
print("=== 坏像素值的偏差分布 ===")
ys, xs = np.where(d > 4)
print(f"  坏像素总数: {len(ys)}")
vals = d[d > 4]
print(f"  偏差: 最小 {vals.min()} 最大 {vals.max()} 中位 {np.median(vals):.0f}")
# 是整像素翻转还是 bit 翻转?
xor = (a ^ b)
for bit in range(16):
    flips = ((xor >> bit) & 1).sum()
    if flips:
        print(f"    bit {bit:2d} 翻转 {flips:6d}")
