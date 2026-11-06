"""精确判定: 暗帧 vs 亮帧 是纯增益变化, 还是内容真的不同。

方法:
  1) 逐像素比值 r = bright/dark, 看 r 的分布 (纯增益 -> r 接近常数)
  2) 相对误差 e = |bright - k*dark| / dark, k = 最优缩放系数
     (纯增益 -> e 接近 0)
  3) 归一化后逐像素差异 (之前用固定阈值 4 会漏掉, 这里用相对阈值)
"""
import numpy as np
from PIL import Image

W, H = 320, 240


def load(p):
    d = open(p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)


def to_img(a):
    r = ((a >> 11) & 0x1F) * 255 // 31
    g = ((a >> 5) & 0x3F) * 255 // 63
    b = (a & 0x1F) * 255 // 31
    return np.dstack([r, g, b]).astype(np.uint8)


names = ["img_0", "img_1", "img_2", "img_3", "img_4", "img_5"]
fs = [(n, load(n + ".bin")) for n in names]
fs = [(n, a) for n, a in fs if a is not None]

print("=== 两两比较: 最优缩放 k 后的残差 ===")
print(f"{'对':14s} {'k':>7s} {'相对残差%':>10s} {'残差>10%占比%':>14s}")
for i in range(len(fs)):
    for j in range(i+1, len(fs)):
        a = fs[i][1].astype(np.float64)
        b = fs[j][1].astype(np.float64)
        k = (a*b).sum() / max((a*a).sum(), 1e-9)
        resid = np.abs(b - k*a) / np.maximum(a, 1.0)
        print(f"{fs[i][0]} vs {fs[j][0]:8s} {k:7.3f} "
              f"{100*resid.mean():9.2f}% {100*(resid>0.10).sum()/(W*H):13.2f}%")

print("\n=== 归一化后逐像素差异 (相对阈值) ===")
for i in range(len(fs)):
    for j in range(i+1, len(fs)):
        a = fs[i][1].astype(np.float64); b = fs[j][1].astype(np.float64)
        na = a/a.mean(); nb = b/b.mean()
        rel = np.abs(nb-na)/np.maximum(na, 1e-6)
        if rel.mean() < 0.02:
            continue
        print(f"  {fs[i][0]} vs {fs[j][0]}: 相对差异均值 {100*rel.mean():6.2f}%  "
              f">20% 占比 {100*(rel>0.2).sum()/(W*H):5.2f}%")

print("\n=== 暗帧/亮帧 的空间差异图 (相对误差) ===")
a = fs[0][1].astype(np.float64); b = fs[1][1].astype(np.float64)
k = (a*b).sum()/max((a*a).sum(), 1e-9)
rel = np.abs(b - k*a)/np.maximum(a, 1.0)
vis = np.clip(rel*5, 0, 255).astype(np.uint8)
Image.fromarray(vis).resize((W*2, H*2), Image.NEAREST).save("relerr.png")
print(f"  已保存 relerr.png  (k={k:.3f}, 平均相对残差 {100*rel.mean():.2f}%)")
print(f"  暗帧均值 {a.mean():.0f}, 亮帧均值 {b.mean():.0f}, 比值 {b.mean()/a.mean():.3f}")
