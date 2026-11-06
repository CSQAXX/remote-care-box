"""
【决定性】亮帧/暗帧 是"纯增益差" 还是 "内容真的不同"?

方法: 把亮帧乘一个系数 k, 再和暗帧算 NCC, 扫描 k。
  - 如果存在某个 k 让 NCC 跳到 >0.95 -> 纯增益差 (光学/曝光)
  - 如果最好的 k 也上不去 0.9 -> 内容真的不同

同时输出并排对比图, 肉眼确认。
"""
import numpy as np
import urllib.request, time, os
from PIL import Image

W, H = 320, 240
IP = "192.168.2.138"
out = r"D:\Software\kanhu\remote-care-box\firmware\out"
os.makedirs(out, exist_ok=True)


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.float64)


def ncc(a, b):
    a = a - a.mean()
    b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 0 else 0.0


def to_rgb(a):
    v = a.astype(np.uint32)
    r = ((v >> 11) & 0x1F) * 255 // 31
    g = ((v >> 5) & 0x3F) * 255 // 63
    b = (v & 0x1F) * 255 // 31
    return np.dstack([r, g, b]).astype(np.uint8)


N = 8
frames = []
for i in range(N):
    frames.append(load(raw()))
    time.sleep(0.15)

lumas = np.array([f.mean() for f in frames])
bi = int(np.argmax(lumas))
di = int(np.argmin(lumas))
print(f"亮帧 #{bi} (luma={lumas[bi]:.0f})   暗帧 #{di} (luma={lumas[di]:.0f})")

print()
print("=== 扫描增益系数 k, 找最佳 NCC ===")
best = (-1, None)
for k in np.arange(0.30, 0.75, 0.01):
    v = ncc(frames[bi].ravel(), (frames[di] * k).ravel())
    if v > best[0]:
        best = (v, k)
    if abs(k * 100 - round(k * 100)) < 1e-9 and int(round(k * 100)) % 5 == 0:
        print(f"  k={k:.2f}: NCC={v:+.4f}")
print(f"  最佳: k={best[1]:.2f} NCC={best[0]:+.4f}")

print()
print("=== 逐行最优 k (每行单独拟合) ===")
rowk = []
for y in range(H):
    b = frames[bi][y]
    d = frames[di][y]
    num = (b * d).sum()
    den = (d * d).sum()
    rowk.append(num / den if den > 0 else 0)
rowk = np.array(rowk)
print(f"  每行最优 k: 中位={np.median(rowk):.3f} 最小={rowk.min():.3f} 最大={rowk.max():.3f}")
print("  前 20 行 k: " + " ".join(f"{rowk[y]:.2f}" for y in range(20)))
print("  后 20 行 k: " + " ".join(f"{rowk[y]:.2f}" for y in range(H - 20, H)))

print()
print("=== 逐像素 k 的一致性 (若内容相同, k 应几乎恒定) ===")
b = frames[bi]
d = frames[di]
mask = d > 2000          # 只看亮一点的像素
kmap = np.zeros_like(d)
kmap[mask] = b[mask] / d[mask]
print(f"  逐像素 k: 中位={np.median(kmap[mask]):.3f} P10={np.percentile(kmap[mask],10):.3f} "
      f"P90={np.percentile(kmap[mask],90):.3f}")

print()
print("=== 并排对比图 (左: 亮帧, 右: 暗帧) ===")
side = np.hstack([to_rgb(frames[bi]), to_rgb(frames[di])])
Image.fromarray(side).save(os.path.join(out, "side_by_side.png"))
# 暗帧放大 2.3 倍后并排
side2 = np.hstack([to_rgb(frames[bi]), to_rgb(np.clip(frames[di] * 2.33, 0, 65535))])
Image.fromarray(side2).save(os.path.join(out, "side_by_side_scaled.png"))
print("  已保存 side_by_side.png / side_by_side_scaled.png")

# 差值图: 亮帧 - 暗帧*k
k = best[1]
diff = frames[bi] - frames[di] * k
print()
print(f"=== 残差统计 (亮帧 - 暗帧*{k:.2f}) ===")
print(f"  残差 绝对值中位 = {np.median(np.abs(diff)):.1f}   P90 = {np.percentile(np.abs(diff),90):.1f}")
print(f"  亮帧本身绝对值中位 = {np.median(np.abs(frames[bi])):.1f}")
