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

frames = [load("cs1.bin"), load("cs2.bin"), load("cs3.bin")]

for i, arr in enumerate(frames):
    Image.fromarray(to_img(arr)).resize((W*2, H*2), Image.NEAREST).save(f"cs{i+1}.png")

print("=== 相邻帧差异 (彩条是静止的, 差异 100% 说明采集链路在随机破坏数据) ===")
for i in range(len(frames)-1):
    a, b = frames[i], frames[i+1]
    d = (a != b)
    print(f"  cs{i+1} vs cs{i+2}: 差异像素 {d.sum()}/{W*H} = {100*d.sum()/(W*H):.2f}%")

print()
print("=== 每列的列内一致性 (彩条是竖条, 同列应完全一致) ===")
for i, arr in enumerate(frames):
    tot_bad = 0
    for x in range(W):
        col = arr[:, x]
        vals, counts = np.unique(col, return_counts=True)
        tot_bad += (col != vals[np.argmax(counts)]).sum()
    print(f"  cs{i+1}: 列内不一致 {tot_bad}/{W*H} = {100*tot_bad/(W*H):.2f}%")

print()
print("=== 三帧之间: 每个像素出现几种不同值 ===")
stacked = np.stack(frames)
uniq = np.zeros((H, W), dtype=np.int32)
for y in range(H):
    for x in range(W):
        uniq[y, x] = len(set(stacked[:, y, x].tolist()))
print(f"  像素取值种类分布: {dict(zip(*np.unique(uniq, return_counts=True)))}")
print(f"  三帧完全一样的像素: {(uniq==1).sum()}/{W*H} = {100*(uniq==1).sum()/(W*H):.2f}%")
print(f"  三帧全不同的像素:   {(uniq==3).sum()}/{W*H} = {100*(uniq==3).sum()/(W*H):.2f}%")

print()
print("=== 彩条内容判定: 取每 40 列的中位色 ===")
med = np.median(stacked, axis=0).astype(int)
for x0 in range(0, W, 40):
    blk = med[:, x0:x0+40].ravel()
    v = int(np.median(blk))
    print(f"  x{x0:3d}-{x0+39:3d}: 0x{v:04X}")
