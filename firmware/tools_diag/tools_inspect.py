import numpy as np
import urllib.request, time
from PIL import Image

W, H = 320, 240

def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

frames = []
for i in range(6):
    d = urllib.request.urlopen("http://192.168.2.138:8080/raw?capture=1", timeout=30).read()
    frames.append(load(d))
    time.sleep(0.25)

b = max(frames, key=lambda f: f.mean())
k = min(frames, key=lambda f: f.mean())

print("=== 亮帧 逐行前 10 像素 ===")
for y in (0, 5, 60, 120, 180, 239):
    print(f"  行{y:3d}: {b[y,:10].tolist()}")
print("=== 暗帧 逐行前 10 像素 ===")
for y in (0, 5, 60, 120, 180, 239):
    print(f"  行{y:3d}: {k[y,:10].tolist()}")

print()
print("=== 亮帧 相邻差 == 2048 的分布 ===")
dh = np.abs(np.diff(b.astype(np.int64), axis=1))
print(f"  2048 出现次数: {(dh==2048).sum()} / {dh.size}")
print(f"  >1000 的比例: {100*(dh>1000).sum()/dh.size:.2f}%")
print(f"  差值直方图 (前 10 个最大的):")
vals, counts = np.unique(dh, return_counts=True)
idx = np.argsort(counts)[::-1][:10]
for i in idx:
    print(f"    差={vals[i]:6d} 出现 {counts[i]:6d} 次 ({100*counts[i]/dh.size:5.2f}%)")

print()
print("=== 亮帧: 是不是只有【偶数/奇数】像素正常 (半像素错位)? ===")
# 假设像素被拆成两个半像素再重新打包 -> 相邻 16 位字的差会呈规律
print("  亮帧 行100 前 20 像素:")
print(f"    {b[100,:20].tolist()}")
print("  暗帧 行100 前 20 像素:")
print(f"    {k[100,:20].tolist()}")

print()
print("=== 亮帧 XOR 亮帧的 bit 统计 ===")
other = max([f for f in frames if f.mean() < 15000], key=lambda f: f.mean())
xor = b ^ other
for bit in range(16):
    n = ((xor >> bit) & 1).sum()
    print(f"  bit{bit:2d}: {n:6d} / {W*H} = {100*n/(W*H):5.2f}%")
