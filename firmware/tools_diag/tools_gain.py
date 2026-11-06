import numpy as np
from PIL import Image

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

a = load("hr0.bin")
b = load("hr1.bin")

print("=== 全局增益检验: b 是不是 a 乘了个系数 (自动曝光)? ===")
a32 = a.astype(np.float64); b32 = b.astype(np.float64)
m = (a32 > 200) & (b32 > 200)   # 只取亮部, 避免黑位偏置
if m.sum() > 1000:
    # 最小二乘 b = k*a
    k = (m * a32 * b32).sum() / (m * a32 * a32).sum()
    pred = k * a32
    resid = np.abs(b32 - pred)[m]
    print(f"  拟合增益 k = {k:.4f}")
    print(f"  残差: 均值 {resid.mean():.1f} 中位 {np.median(resid):.1f} p90 {np.percentile(resid,90):.1f}")
    print(f"  残差 < 8 的比例: {100*(resid<8).mean():.2f}%")
else:
    print("  亮部样本太少")

print()
print("=== 每个通道的量 ===")
for nm, arr in (("帧0", a), ("帧1", b)):
    r = (arr >> 11) & 0x1F; g = (arr >> 5) & 0x3F; bl = arr & 0x1F
    print(f"  {nm}: R均{np.mean(r):5.2f} G均{np.mean(g):6.2f} B均{np.mean(bl):5.2f}  "
          f"R中{np.median(r):4.1f} G中{np.median(g):5.1f} B中{np.median(bl):4.1f}")

print()
print("=== 逐像素差 vs 像素值 (散点统计) ===")
d = np.abs(a - b)
for lo, hi in [(0, 64), (64, 512), (512, 4096), (4096, 32768), (32768, 65536)]:
    sel = (a >= lo) & (a < hi)
    if sel.sum() > 0:
        print(f"  像素值[{lo:5d},{hi:5d}): n={sel.sum():6d}  平均差 {d[sel].mean():7.2f}  "
              f"最大差 {d[sel].max():6d}")

print()
print("=== 亮部(=场景主体)的差异图 ===")
vis = np.zeros((H, W, 3), np.uint8)
vis[..., 0] = np.clip(d * 4, 0, 255)
Image.fromarray(vis).resize((W*2, H*2), Image.NEAREST).save("hr_diff.png")
print("  saved hr_diff.png")

print()
print("=== 关键: 画面主体区域(亮部)的像素是否只是整体变亮? ===")
bright = a > 8000
if bright.sum() > 100:
    print(f"  亮部像素 {bright.sum()} 个, 其中差异>4 的比例 {100*(d[bright]>4).mean():.2f}%")
    print(f"  暗部像素 {(~bright).sum()} 个, 其中差异>4 的比例 {100*(d[~bright]>4).mean():.2f}%")
