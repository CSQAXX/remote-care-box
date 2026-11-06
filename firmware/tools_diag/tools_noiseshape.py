"""
把"亮帧里的高频噪声"可视化, 判断它到底是什么形态:
  (a) 随机雪花 (salt&pepper) -> 采集链路 bit 错误
  (b) 横向条纹/条带          -> 行丢失/撕裂
  (c) 规则网格/棋盘          -> 位打包或半像素错位
"""
import numpy as np
import urllib.request, time
from PIL import Image

W, H = 320, 240

def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

frames = []
for i in range(8):
    d = urllib.request.urlopen("http://192.168.2.138:8080/raw?capture=1", timeout=30).read()
    frames.append(load(d))
    time.sleep(0.25)

b = max(frames, key=lambda f: f.mean())
k = min(frames, key=lambda f: f.mean())
print(f"亮帧亮度 {b.mean():.0f}, 暗帧亮度 {k.mean():.0f}")

def to_img(arr):
    r = ((arr >> 11) & 0x1F) * 255 // 31
    g = ((arr >> 5) & 0x3F) * 255 // 63
    bl = (arr & 0x1F) * 255 // 31
    return np.dstack([r, g, bl]).astype(np.uint8)

Image.fromarray(to_img(b)).resize((W*2, H*2), Image.NEAREST).save("v_bright.png")
Image.fromarray(to_img(k)).resize((W*2, H*2), Image.NEAREST).save("v_dark.png")

# salt&pepper mask: 与 3x3 中值差很大
def median3(a):
    p = np.pad(a, 1, mode="edge")
    st = np.stack([p[dy:dy+H, dx:dx+W] for dy in range(3) for dx in range(3)], axis=0)
    return np.median(st, axis=0)

mb = np.abs(b - median3(b))
mk = np.abs(k - median3(k))
print(f"亮帧: |像素-3x3中值| 的 P50={np.percentile(mb,50):.0f} P90={np.percentile(mb,90):.0f} P99={np.percentile(mb,99):.0f} max={mb.max()}")
print(f"暗帧: |像素-3x3中值| 的 P50={np.percentile(mk,50):.0f} P90={np.percentile(mk,90):.0f} P99={np.percentile(mk,99):.0f} max={mk.max()}")

for nm, m in (("bright", mb), ("dark", mk)):
    vis = np.zeros((H, W, 3), np.uint8)
    vis[..., 0] = np.clip(m * 20, 0, 255)
    Image.fromarray(vis).resize((W*2, H*2), Image.NEAREST).save(f"mask_{nm}.png")

print()
print("=== 噪声的空间结构 ===")
# 按行统计噪声总量 -> 条纹?
row_noise = (mb > 256).sum(axis=1)
print("  亮帧 每行噪声像素数 (每 16 行):")
for y in range(0, H, 16):
    print(f"    行{y:3d}-{y+15:3d}: " + " ".join(f"{v:3d}" for v in row_noise[y:y+16]))
col_noise = (mb > 256).sum(axis=0)
print(f"  每列噪声数: 均值 {col_noise.mean():.1f} 最大 {col_noise.max()} 最小 {col_noise.min()}")

print()
print("=== 噪声是否集中在特定 bit? (对亮帧做 3x3 中值差, 看每 bit 的翻转率) ===")
diff = b ^ median3(b).astype(np.int32)
for bit in range(16):
    n = ((diff >> bit) & 1).sum()
    bar = "#" * int(60 * n / (W * H))
    print(f"  bit{bit:2d}: {n:6d} ({100*n/(W*H):5.2f}%) {bar}")

print()
print("=== 亮帧 vs 暗帧: 掩掉某些 bit 后差异是否变小 ===")
xor = b ^ k
for mask_desc, mask in (("全部", 0xFFFF), ("去掉 bit15/14/13 (R高3位)", 0x1FFF),
                        ("去掉 bit15/14 (R高2位)", 0x3FFF),
                        ("去掉 bit15 (R最高位)", 0x7FFF),
                        ("只留 bit4..0 (B)", 0x001F),
                        ("只留 bit10..5 (G)", 0x07E0),
                        ("只留 bit15..11 (R)", 0xF800)):
    x = xor & mask
    n = sum(((x >> b_) & 1).sum() for b_ in range(16))
    print(f"  {mask_desc:24s}: 差异bit {n:7d} ({100*n/(W*H):5.2f}%)")
print("saved v_bright.png v_dark.png mask_bright.png mask_dark.png")
