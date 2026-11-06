import numpy as np
from PIL import Image
import glob, re

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

def stats(a, b, label):
    if a is None or b is None:
        print(f"  {label}: missing"); return
    d = np.abs(a - b)
    print(f"  {label}: 差异 {100*(a!=b).sum()/(W*H):.2f}%  "
          f"差>4 {100*(d>4).sum()/(W*H):.2f}%  差>100 {100*(d>100).sum()/(W*H):.2f}%")
    xor = a ^ b
    flips = sum(((xor >> bit) & 1).sum() for bit in range(16))
    print(f"          bit翻转率 {100*flips/16/(W*H):.2f}%")

print("=== 彩条 (静止) ===")
cb = [load(f"win_cb{i}.bin") for i in (1,2,3)]
stats(cb[0], cb[1], "cb1 vs cb2")
stats(cb[1], cb[2], "cb2 vs cb3")

print("\n=== 真实场景 ===")
sc = [load(f"win_sc{i}.bin") for i in (1,2,3)]
stats(sc[0], sc[1], "sc1 vs sc2")
stats(sc[1], sc[2], "sc2 vs sc3")

for tag, arr in zip(("cb", "sc"), (cb[1], sc[1])):
    if arr is not None:
        Image.fromarray(to_img(arr)).resize((W*2, H*2), Image.NEAREST).save(f"win_{tag}.png")
print("saved win_cb.png win_sc.png")
