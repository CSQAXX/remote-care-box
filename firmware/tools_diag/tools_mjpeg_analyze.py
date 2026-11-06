"""数值分析 MJPEG 帧: 查横向撕裂条带 / 彩色条纹 / 位翻转。"""
from PIL import Image
import glob, os, math

d = 'mjpeg_dump'
fs = sorted(glob.glob(os.path.join(d, '*.jpg')))
ims = [Image.open(f).convert('RGB') for f in fs]
w, h = ims[0].size
print(f"帧数={len(ims)} 尺寸={w}x{h}")

# ---- 1. 逐行亮度, 找突变行 (横向撕裂条带的特征) ----
print("\n=== 1. 逐行亮度突变检测 (撕裂条带) ===")
for idx in (0, 5, 20, 60):
    im = ims[idx]
    px = im.load()
    rows = []
    for y in range(h):
        s = 0
        for x in range(0, w, 4):
            r, g, b = px[x, y]
            s += (r + g + b) / 3
        rows.append(s / (w // 4))
    jumps = []
    for y in range(1, h):
        d_ = abs(rows[y] - rows[y - 1])
        if d_ > 18:
            jumps.append((y, round(d_, 1)))
    print(f"  frame {idx:3d}: 行均值范围 {min(rows):.0f}~{max(rows):.0f}, "
          f"突变行 {len(jumps)} 处 {jumps[:8]}")

# ---- 2. 彩色条纹: 按列统计色相一致性 ----
print("\n=== 2. 逐行色度 (彩条/偏色的特征) ===")
for idx in (0, 5, 20, 60):
    im = ims[idx]
    px = im.load()
    bad = []
    for y in range(h):
        rs = gs = bs = 0
        n = 0
        for x in range(0, w, 2):
            r, g, b = px[x, y]
            rs += r; gs += g; bs += b; n += 1
        r, g, b = rs / n, gs / n, bs / n
        # 饱和度异常高 = 该行是纯彩色条
        mx, mn = max(r, g, b), min(r, g, b)
        if mx > 100 and (mx - mn) / mx > 0.6:
            bad.append((y, (int(r), int(g), int(b))))
    print(f"  frame {idx:3d}: 高饱和行 {len(bad)} 处 {bad[:6]}")

# ---- 3. 全局色彩分布 ----
print("\n=== 3. 全局色彩分布 (frame 20) ===")
im = ims[20]
px = im.load()
hist = {}
for y in range(0, h, 3):
    for x in range(0, w, 3):
        p = px[x, y]
        key = (p[0] // 32 * 32, p[1] // 32 * 32, p[2] // 32 * 32)
        hist[key] = hist.get(key, 0) + 1
tot = sum(hist.values())
top = sorted(hist.items(), key=lambda x: -x[1])[:8]
for k, v in top:
    print(f"  RGB{k}: {v*100//tot}%")

# ---- 4. 相邻帧差异 (撕裂会表现为局部大差异) ----
print("\n=== 4. 相邻帧差异 ===")
for i in range(0, min(6, len(ims) - 1)):
    a, b = ims[i], ims[i + 1]
    pa, pb = a.load(), b.load()
    tot_d = 0
    max_d = 0
    n = 0
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            d_ = sum(abs(pa[x, y][k] - pb[x, y][k]) for k in range(3))
            tot_d += d_
            max_d = max(max_d, d_)
            n += 1
    print(f"  {i}->{i+1}: 平均差 {tot_d/n:.1f}, 最大差 {max_d}")
