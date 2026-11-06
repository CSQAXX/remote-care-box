import numpy as np
from PIL import Image

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

a = load("sc1.bin")
b = load("sc2.bin")

def diff_pct(x, y):
    return 100 * (x != y).sum() / (W * H)

print("raw                    :", f"{diff_pct(a, b):.2f}%")
bs = ((b & 0xFF) << 8) | (b >> 8)
print("b byteswapped          :", f"{diff_pct(a, bs):.2f}%")
print("a byteswapped vs b     :", f"{diff_pct(((a & 0xFF) << 8) | (a >> 8), b):.2f}%")

print("\n--- 水平移位对齐 ---")
for s in range(-3, 4):
    if s == 0:
        continue
    aa = a[:, max(0, s):W + min(0, s)]
    bb = b[:, max(0, -s):W + min(0, -s)]
    print(f"  shift {s:+d}: {diff_pct(aa, bb):.2f}%")

print("\n--- 垂直移位对齐 ---")
for s in range(-3, 4):
    if s == 0:
        continue
    aa = a[max(0, s):H + min(0, s), :]
    bb = b[max(0, -s):H + min(0, -s), :]
    print(f"  shift {s:+d}: {diff_pct(aa, bb):.2f}%")

print("\n--- 逐行比较 (看看是不是整行错位) ---")
for y in range(0, H, 16):
    print(f"  row {y:3d}: a[0:6]={a[y,:6].tolist()}  b[0:6]={b[y,:6].tolist()}")

print("\n--- 统计: 两帧每个像素的差值分布 ---")
d = np.abs(a - b)
print(f"  ==0        : {(d==0).sum():6d} ({100*(d==0).sum()/(W*H):.2f}%)")
print(f"  1..31      : {((d>=1)&(d<32)).sum():6d} ({100*((d>=1)&(d<32)).sum()/(W*H):.2f}%)")
print(f"  32..255    : {((d>=32)&(d<256)).sum():6d} ({100*((d>=32)&(d<256)).sum()/(W*H):.2f}%)")
print(f"  >=256      : {(d>=256).sum():6d} ({100*(d>=256).sum()/(W*H):.2f}%)")

# 差异可视化
vis = np.zeros((H, W, 3), np.uint8)
vis[..., 0] = np.clip(d * 4, 0, 255)
Image.fromarray(vis).resize((W*2, H*2), Image.NEAREST).save("diff_vis.png")
print("saved diff_vis.png")
