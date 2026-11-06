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
    return np.dstack([r, g, bl] if False else [r, g, b]).astype(np.uint8)

def bitflip(a, b):
    xor = a ^ b
    return 100 * sum(((xor >> bit) & 1).sum() for bit in range(16)) / 16 / (W * H)

print("=== 彩条 (静止图样) —— bit 翻转率就是纯链路噪声 ===")
cb = [load(f"fin_cb{i}.bin") for i in (1, 2, 3)]
for i in range(2):
    a, b = cb[i], cb[i+1]
    if a is None or b is None: continue
    d = np.abs(a - b)
    print(f"  fin_cb{i+1} vs fin_cb{i+2}: bit翻转率 {bitflip(a,b):6.2f}%   "
          f"差>4 {100*(d>4).sum()/(W*H):5.2f}%   差>100 {100*(d>100).sum()/(W*H):5.2f}%")

print("\n=== 真实场景 (归一化掉自动曝光) ===")
sc = [load(f"fin_sc{i}.bin") for i in (1, 2, 3)]
for i in range(2):
    a, b = sc[i], sc[i+1]
    if a is None or b is None: continue
    na = a / (a.mean() or 1)
    nb = b / (b.mean() or 1)
    d = np.abs(na - nb)
    print(f"  fin_sc{i+1} vs fin_sc{i+2}: 归一化后 差异>4 {100*(d>4).sum()/(W*H):5.2f}%  "
          f"差异>100 {100*(d>100).sum()/(W*H):5.2f}%   (原始 bit翻转 {bitflip(a,b):.2f}%)")

for i, arr in enumerate(sc):
    if arr is not None:
        Image.fromarray(to_img(arr)).resize((W*2, H*2), Image.NEAREST).save(f"fin_sc{i+1}.png")
if cb[1] is not None:
    Image.fromarray(to_img(cb[1])).resize((W*2, H*2), Image.NEAREST).save("fin_cb2.png")
print("\nsaved fin_sc1..3.png fin_cb2.png")
