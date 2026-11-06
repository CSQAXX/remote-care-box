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

for tag in ("sc1", "sc2", "sc3"):
    a = load(f"{tag}.bin")
    Image.fromarray(to_img(a)).resize((W*2, H*2), Image.NEAREST).save(f"{tag}.png")
    print(tag, "row0 first 10:", a[0, :10].tolist())

a = load("sc1.bin"); b = load("sc2.bin")
d = np.abs(a.astype(np.int32) - b.astype(np.int32))
print("\nper-row: fraction of pixels with diff>100")
for y in range(0, H, 24):
    row = d[y]
    print(f"  row {y:3d}: >100 = {(row>100).sum():3d}/{W}   maxdiff={row.max():5d}")
