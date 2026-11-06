import numpy as np
from PIL import Image

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

a = load("cbhttp0.bin")
b = load("cbhttp1.bin")
c = load("cbhttp2.bin")

print("=== 每一行的众数 (行内应当完全一致, 彩条是横条) ===")
print("  行  | 帧0 众数      | 帧1 众数      | 帧2 众数      | 帧0行内不同值数")
for y in range(0, H, 8):
    out = []
    for arr in (a, b, c):
        vals, counts = np.unique(arr[y], return_counts=True)
        out.append((int(vals[np.argmax(counts)]), int(counts.max())))
    u0 = len(np.unique(a[y]))
    print(f"  {y:3d} | 0x{out[0][0]:04X} x{out[0][1]:3d} | 0x{out[1][0]:04X} x{out[1][1]:3d} | "
          f"0x{out[2][0]:04X} x{out[2][1]:3d} | {u0}")

print()
print("=== 帧内逐行众数序列 (应该有清晰的台阶) ===")
for nm, arr in (("帧0", a), ("帧2", c)):
    seq = []
    for y in range(H):
        vals, counts = np.unique(arr[y], return_counts=True)
        seq.append(int(vals[np.argmax(counts)]))
    # 压缩成连续段
    segs = []
    s = 0
    for i in range(1, H + 1):
        if i == H or seq[i] != seq[s]:
            segs.append((s, i-1, seq[s]))
            s = i
    print(f"  {nm}: {len(segs)} 段")
    for st, en, v in segs:
        print(f"     行{st:3d}-{en:3d}: 0x{v:04X}")
