"""
【决定性】暗帧是不是亮帧的某种"比特变换"?

候选假设 (逐个验证):
  H1: 暗帧 = 亮帧 >> N (整帧右移 N 位)
  H2: 暗帧 = 亮帧的字节错位 (整体左移/右移 1~3 字节)
  H3: 暗帧 = 亮帧的色位域被重新排列 (R/G/B 交换, 或位段偏移)
  H4: 暗帧 = 亮帧的列错位 (X 方向偏移)
  H5: 暗帧 = 亮帧的行错位 (Y 方向偏移, 大范围扫描)

如果某个假设成立, 那就说明帧内容被【重新解释】了 ——
这是"格式/打包/对齐"层面的 bug, 而不是光学噪声。
"""
import numpy as np
import urllib.request, time, os
from PIL import Image

W, H = 320, 240
IP = "192.168.2.138"
out = r"D:\Software\kanhu\remote-care-box\firmware\out"


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int64)


def to_rgb(a):
    v = np.clip(a, 0, 65535).astype(np.uint32)
    r = ((v >> 11) & 0x1F) * 255 // 31
    g = ((v >> 5) & 0x3F) * 255 // 63
    b = (v & 0x1F) * 255 // 31
    return np.dstack([r, g, b]).astype(np.uint8)


N = 14
frames = []
for i in range(N):
    frames.append(load(raw()))
    time.sleep(0.15)

lum = np.array([f.mean() for f in frames])
med = np.median(lum)
bi = int(np.argmax(lum))
di = int(np.argmin(lum))
B, D = frames[bi], frames[di]
print(f"亮帧#{bi} luma={lum[bi]:.0f}   暗帧#{di} luma={lum[di]:.0f}")

# 输出图像供肉眼判断
Image.fromarray(to_rgb(B)).save(os.path.join(out, "X_bright.png"))
Image.fromarray(to_rgb(D)).save(os.path.join(out, "X_dark.png"))

print()
print("=== H1: 整帧右移 N 位 ===")
for n in range(1, 16):
    v = (B >> n)
    err = np.abs(v.astype(np.float64) - D.astype(np.float64)).mean()
    print(f"  >>{n:2d}: 平均绝对误差={err:9.1f}")
print(f"  基线 (不移位): {np.abs(B.astype(np.float64)-D.astype(np.float64)).mean():9.1f}")

print()
print("=== H1b: 整帧左移 N 位 ===")
for n in range(1, 16):
    v = (B << n) & 0xFFFF
    err = np.abs(v.astype(np.float64) - D.astype(np.float64)).mean()
    print(f"  <<{n:2d}: 平均绝对误差={err:9.1f}")

print()
print("=== H2: 字节错位 ===")
Bb = B.astype(np.uint16).view(np.uint8).reshape(H, W * 2)
Db = D.astype(np.uint16).view(np.uint8).reshape(H, W * 2)
base = np.abs(Bb.astype(np.int32) - Db.astype(np.int32)).mean()
print(f"  基线: {base:.2f}")
for s in (-3, -2, -1, 1, 2, 3):
    if s > 0:
        v = np.roll(Bb, s, axis=1)
    else:
        v = np.roll(Bb, s, axis=1)
    v[:, :s] = 0 if s < 0 else v[:, :s]
    err = np.abs(v.astype(np.int32) - Db.astype(np.int32)).mean()
    print(f"  移位 {s:+d}: 误差={err:.2f}")

print()
print("=== H3: 色位域重排 ===")


def unpack(v):
    return ((v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F)


for name, perm in [("R<->B", (2, 1, 0)), ("R<->G", (1, 0, 2)), ("G<->B", (0, 2, 1)),
                   ("同序", (0, 1, 2))]:
    r, g, b = unpack(B)
    ch = [r, g, b]
    v = (ch[perm[0]] << 11) | (ch[perm[1]] << 5) | ch[perm[2]]
    err = np.abs(v.astype(np.float64) - D.astype(np.float64)).mean()
    print(f"  {name}: 误差={err:9.1f}")
# G3/G2 内部交换
r, g, b = unpack(B)
v = (r << 11) | ((g & 0x38) | ((g & 0x07) << 3)) | b
err = np.abs(v.astype(np.float64) - D.astype(np.float64)).mean()
print(f"  G 高低 3 位互换: 误差={err:9.1f}")
# 位段整体错位: R5 的 5 位挪到不同位置
for sh in range(1, 6):
    v = ((B >> 11) << (11 + sh)) & 0xFFFF
    err = np.abs(v.astype(np.float64) - D.astype(np.float64)).mean()
    print(f"  R5 左移 {sh} 位: 误差={err:9.1f}")

print()
print("=== H4: 列错位 (X 偏移) ===")
base = np.abs(B.astype(np.float64) - D.astype(np.float64)).mean()
print(f"  基线: {base:.1f}")
best = (base, 0)
for s in range(-40, 41):
    if s >= 0:
        v = np.roll(B, s, axis=1)
    else:
        v = np.roll(B, s, axis=1)
    err = np.abs(v.astype(np.float64) - D.astype(np.float64)).mean()
    if err < best[0]:
        best = (err, s)
print(f"  最佳列偏移: {best[1]:+d} 误差={best[0]:.1f} (改善 {base/best[0]:.2f}x)")

print()
print("=== H5: 行错位 (Y 偏移, 大范围) ===")
best = (base, 0)
for s in range(-239, 240):
    v = np.roll(B, s, axis=0)
    err = np.abs(v.astype(np.float64) - D.astype(np.float64)).mean()
    if err < best[0]:
        best = (err, s)
print(f"  最佳行偏移: {best[1]:+d} 误差={best[0]:.1f} (改善 {base/best[0]:.2f}x)")
