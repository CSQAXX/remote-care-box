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
    return np.dstack([r, g, b]).astype(np.uint8)

print("=== 场景帧亮度 (是否只是自动曝光在变) ===")
for i in (1, 2, 3):
    a = load(f"win_sc{i}.bin")
    if a is None:
        print(f"  win_sc{i}: missing"); continue
    Image.fromarray(to_img(a)).resize((W*2, H*2), Image.NEAREST).save(f"win_sc{i}.png")
    print(f"  win_sc{i}: mean={a.mean():8.1f}  median={np.median(a):8.1f}  "
          f"R5={((a>>11)&0x1F).mean():5.2f} G6={((a>>5)&0x3F).mean():5.2f} B5={(a&0x1F).mean():5.2f}")

print()
print("=== 归一化后再比 (消掉曝光变化) ===")
sc = [load(f"win_sc{i}.bin") for i in (1, 2, 3)]
for i in range(2):
    a, b = sc[i].astype(np.float64), sc[i+1].astype(np.float64)
    na = a / (a.mean() or 1)
    nb = b / (b.mean() or 1)
    d = np.abs(na - nb)
    print(f"  sc{i+1} vs sc{i+2}: 归一化后 差异>4 = {100*(d>4).sum()/(W*H):.2f}%  "
          f"差异>100 = {100*(d>100).sum()/(W*H):.2f}%")

print()
print("=== 彩条帧亮度 ===")
for i in (1, 2, 3):
    a = load(f"win_cb{i}.bin")
    if a is not None:
        print(f"  win_cb{i}: mean={a.mean():8.1f} median={np.median(a):8.1f}")
