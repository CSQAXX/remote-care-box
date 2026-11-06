import numpy as np
from PIL import Image
import os

W, H = 320, 240

def load(p):
    d = open(p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

def to_img(arr):
    r = ((arr >> 11) & 0x1F) * 255 // 31
    g = ((arr >> 5) & 0x3F) * 255 // 63
    bl = (arr & 0x1F) * 255 // 31
    return np.dstack([r, g, bl]).astype(np.uint8)

frames = []
for i in range(5):
    p = f"hr{i}.bin"
    if os.path.exists(p):
        frames.append((p, load(p)))
        Image.fromarray(to_img(frames[-1][1])).resize((W*2, H*2), Image.NEAREST).save(f"hr{i}.png")

print("=== HTTP 抓帧 (无串口干扰) 的帧间差异 ===")
for i in range(len(frames) - 1):
    a = frames[i][1]; b = frames[i+1][1]
    d = (a != b)
    ad = np.abs(a - b)
    xor = (a ^ b)
    flips = sum(((xor >> k) & 1).sum() for k in range(16))
    print(f"  {frames[i][0]} vs {frames[i+1][0]}: 差异 {100*d.sum()/(W*H):6.2f}%  "
          f"bit翻转 {100*flips/16/(W*H):5.2f}%  差>4 {100*(ad>4).sum()/(W*H):6.2f}%  "
          f"差>100 {100*(ad>100).sum()/(W*H):6.2f}%")

print()
print("=== 串口抓帧 vs HTTP 抓帧 的差异 (验证串口是否污染了数据) ===")
if os.path.exists("sc1.bin"):
    s1 = load("sc1.bin")
    h0 = frames[0][1]
    print(f"  sc1(串口) vs hr0(HTTP): {100*(s1!=h0).sum()/(W*H):.2f}% 像素不同")
    print(f"  sc1 首行: {s1[0,:8].tolist()}")
    print(f"  hr0 首行: {h0[0,:8].tolist()}")

print()
print("=== HTTP 帧的列内一致性 (每列应几乎一致, 彩色场景除外) ===")
a = frames[0][1]
dh = np.abs(np.diff(a.astype(np.int32), axis=1))
print(f"  行内相邻差: 均值 {dh.mean():.2f} 中位 {np.median(dh):.2f} 最大 {dh.max()}")
rowmed = np.median(dh, axis=1)
print(f"  各行中位差: 最小 {rowmed.min():.2f} 最大 {rowmed.max():.2f} (行 {rowmed.argmax()})")
