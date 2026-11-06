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

def metrics(arr, label):
    dh = np.abs(np.diff(arr.astype(np.int32), axis=1))
    # 行内是否平整 (彩条是横条, 同一行内所有像素应该完全一样)
    rowflat = 0
    for y in range(H):
        u = np.unique(arr[y])
        if len(u) == 1:
            rowflat += 1
    print(f"  {label:16s} 行内相邻差 最大={dh.max():6d}  完全平整的行数={rowflat}/{H} ({100*rowflat/H:.1f}%)")
    return rowflat

tags = ["base", "pda0", "pclk2", "pclk3", "mode1",
        "pda0_pclk1", "pda0_pclk2", "pda0_pclk3", "pda1_pclk3", "pda0_pclk0"]
print("=== 彩条(横条) 下的行平整度 ===")
for t in tags:
    p = f"t_{t}_a.bin"
    if not os.path.exists(p):
        continue
    a = load(p)
    if a is None:
        continue
    metrics(a, t)

print()
print("=== 真实场景的帧间噪声 (抓 3 帧) ===")
for i in (1, 2, 3):
    p = f"sc{i}.bin"
    if os.path.exists(p):
        a = load(p)
        Image.fromarray(to_img(a)).resize((W*2, H*2), Image.NEAREST).save(f"sc{i}.png")
print("  (场景图已刷新)")
