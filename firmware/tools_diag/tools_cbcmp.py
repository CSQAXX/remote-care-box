import numpy as np
from PIL import Image

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

a = load("cb_a.bin")
b = load("cb_b.bin")

def to_img(arr):
    r = ((arr >> 11) & 0x1F) * 255 // 31
    g = ((arr >> 5) & 0x3F) * 255 // 63
    bl = (arr & 0x1F) * 255 // 31
    return np.dstack([r, g, bl]).astype(np.uint8)

Image.fromarray(to_img(a)).resize((W*2, H*2), Image.NEAREST).save("cb_a.png")
Image.fromarray(to_img(b)).resize((W*2, H*2), Image.NEAREST).save("cb_b.png")

for nm, arr in (("A", a), ("B", b)):
    print(f"--- frame {nm}: per-row dominant value / distinct count ---")
    for y in range(0, H, 24):
        vals, counts = np.unique(arr[y], return_counts=True)
        dom = vals[np.argmax(counts)]
        print(f"   row {y:3d}: distinct={len(vals):3d}  dominant=0x{dom:04X} x{counts.max():3d}")

d = (a != b)
print(f"\nA vs B differing pixels: {d.sum()}/{W*H} = {100*d.sum()/(W*H):.2f}%")
