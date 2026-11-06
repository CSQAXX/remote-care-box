import numpy as np
from PIL import Image

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

def to_img(arr):
    r = ((arr >> 11) & 0x1F) * 255 // 31
    g = ((arr >> 5) & 0x3F) * 255 // 63
    bl = (arr & 0x1F) * 255 // 31
    return np.dstack([r, g, bl]).astype(np.uint8)

for i in range(4):
    a = load(f"cbhttp{i}.bin")
    Image.fromarray(to_img(a)).resize((W*2, H*2), Image.NEAREST).save(f"cbh{i}.png")

a = load("cbhttp0.bin")
b = load("cbhttp2.bin")
print("帧0 前3行前12像素:", a[0,:12].tolist())
print("帧2 前3行前12像素:", b[0,:12].tolist())
print()
print("=== 每 40 列的中位色 (彩条应该是纯色块) ===")
for i, arr in enumerate([a, b]):
    print(f" 帧{i}:", [f"0x{int(np.median(arr[:, x:x+40])):04X}" for x in range(0, W, 40)])
print()
print("=== 帧内: 同一列的取值分布 (第 100 列) ===")
for i, arr in enumerate([a, b]):
    col = arr[:, 100]
    vals, counts = np.unique(col, return_counts=True)
    print(f" 帧{i}: {len(vals)} 种取值, top5={[(f'0x{v:04X}', c) for v, c in sorted(zip(vals, counts), key=lambda t: -t[1])[:5]]}")
