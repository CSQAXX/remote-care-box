import numpy as np

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

fs = [load(f"cbhttp{i}.bin") for i in range(4)]

def rowvec(a, y):
    return a[y]

print("=== 帧间垂直移位搜索 (在 128-239 干净区找最佳对齐) ===")
for i in range(len(fs) - 1):
    a, b = fs[i], fs[i+1]
    best = None
    for s in range(-40, 41):
        y0a, y0b = 140, 140 - s
        if y0a < 0 or y0a + 80 > H or y0b < 0 or y0b + 80 > H:
            continue
        aa = a[y0a:y0a+80, :]
        bb = b[y0b:y0b+80, :]
        err = np.abs(aa - bb).mean()
        if best is None or err < best[1]:
            best = (s, err)
    print(f"  帧{i} vs 帧{i+1}: 最佳垂直移位 = {best[0]:+d} 行, 平均绝对误差 {best[1]:.2f}")

print()
print("=== 帧内: 同一色带内, 每行的内容是否一致 (行号 130-155) ===")
a = fs[0]
for y in range(128, 160, 2):
    u = len(np.unique(a[y]))
    print(f"  行{y:3d}: 不同值数={u:3d}  首8={a[y,:8].tolist()}")

print()
print("=== 帧0 vs 帧1 在最佳移位后的整体差异 ===")
a, b = fs[0], fs[1]
for s in (0, -1, 1, -2, 2, -16, 16):
    y0a, y0b = 140, 140 - s
    if y0a < 0 or y0a + 90 > H or y0b < 0 or y0b + 90 > H:
        continue
    aa = a[y0a:y0a+90, :]
    bb = b[y0b:y0b+90, :]
    print(f"  shift {s:+3d}: 差异 {100*(aa!=bb).sum()/(90*W):6.2f}%")
