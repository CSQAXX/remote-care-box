import numpy as np

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

fs = [load(f"cbhttp{i}.bin") for i in range(4)]

print("=== 帧0 各行是否彼此完全相同 (纯水平渐变则应相同) ===")
a = fs[0]
base = a[140]
for y in (0, 32, 64, 96, 128, 140, 160, 190, 220, 239):
    print(f"  行{y:3d} vs 行140: 差异 {100*(a[y]!=base).sum()/W:6.2f}%   首8={a[y,:8].tolist()}")

print()
print("=== 帧内 水平渐变 的连续性: 相邻列差 (行140) ===")
row = fs[0][140].astype(np.int32)
d = np.abs(np.diff(row))
print(f"  min={d.min()} max={d.max()} mean={d.mean():.2f}")
print(f"  前 40 个相邻差: {d[:40].tolist()}")

print()
print("=== 帧间 水平移位搜索 (行140, 纯渐变 -> 移位后应完全吻合) ===")
for i in range(len(fs) - 1):
    a, b = fs[i][140], fs[i+1][140]
    best = None
    for s in range(-64, 65):
        x0a, x0b = 64, 64 - s
        if x0a < 0 or x0a + 128 > W or x0b < 0 or x0b + 128 > W:
            continue
        err = np.abs(a[x0a:x0a+128].astype(np.int32) - b[x0b:x0b+128].astype(np.int32)).mean()
        if best is None or err < best[1]:
            best = (s, err)
    err0 = np.abs(a[64:192].astype(np.int32) - b[64:192].astype(np.int32)).mean()
    print(f"  帧{i} vs 帧{i+1}: 无移位误差={err0:9.2f}   最佳移位={best[0]:+3d} 误差={best[1]:9.2f}")

print()
print("=== 帧间 垂直移位搜索 (用列 x=100 的渐变, 该列沿行也应渐变) ===")
for i in range(len(fs) - 1):
    a = fs[i][:, 100].astype(np.int32)
    b = fs[i+1][:, 100].astype(np.int32)
    best = None
    for s in range(-32, 33):
        y0a, y0b = 100, 100 - s
        if y0a < 0 or y0a + 80 > H or y0b < 0 or y0b + 80 > H:
            continue
        err = np.abs(a[y0a:y0a+80] - b[y0b:y0b+80]).mean()
        if best is None or err < best[1]:
            best = (s, err)
    print(f"  帧{i} vs 帧{i+1}: 最佳垂直移位={best[0]:+3d} 行, 误差={best[1]:.2f}")
