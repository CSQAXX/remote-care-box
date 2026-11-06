"""
决定性分析: "亮帧" 和 "暗帧" 到底差在哪?

已知: 两者内容看起来都是同一个房间, 但亮度差 ~2 倍, 且缩放后仍有 78% 像素差 >10%。

要回答的问题: 差异集中在【哪个位域】?
  R5 = (v>>11)&31   (bit 15..11)
  G3 = (v>>8)&7     (bit 10..8)
  G2 = (v>>5)&7     (bit 7..5)
  B5 = v&31         (bit 4..0)

如果差异只在某一个位域 -> 直接指向那一级的硬件/配置问题。
如果四个位域都均匀地变 -> 就是【整体曝光不同】, 属于传感器行为。
"""
import numpy as np
import urllib.request, time, os

W, H = 320, 240
IP = "192.168.2.138"
os.chdir(r"D:\Software\kanhu\remote-care-box\firmware")


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int64)


frames = []
for i in range(12):
    frames.append(load(raw()))
    time.sleep(0.25)

lums = [f.mean() for f in frames]
print("各帧亮度:", [f"{v:.0f}" for v in lums])

bi = int(np.argmax(lums))
di = int(np.argmin(lums))
B, D = frames[bi], frames[di]
print(f"\n亮帧#{bi} 亮度{B.mean():.0f}   暗帧#{di} 亮度{D.mean():.0f}")

names = ["R5(bit15-11)", "G3(bit10-8)", "G2(bit7-5)", "B5(bit4-0)"]
shifts = [11, 8, 5, 0]
masks = [0x1F, 0x07, 0x07, 0x1F]

print("\n=== 各位域的统计 ===")
print(f"{'位域':14s} {'亮帧均值':>9s} {'暗帧均值':>9s} {'差':>7s} "
      f"{'逐像素相同%':>12s} {'差<=1的%':>10s}")
for nm, sh, mk in zip(names, shifts, masks):
    b = (B >> sh) & mk
    d = (D >> sh) & mk
    same = 100 * (b == d).mean()
    close = 100 * (np.abs(b.astype(int) - d.astype(int)) <= 1).mean()
    print(f"{nm:14s} {b.mean():9.3f} {d.mean():9.3f} {b.mean()-d.mean():+7.3f} "
          f"{same:11.2f}% {close:9.2f}%")

print("\n=== 若把亮帧的 R5 减 1, 能不能对上暗帧? ===")
b_r5 = (B >> 11) & 0x1F
d_r5 = (D >> 11) & 0x1F
for delta in (-2, -1, 0, 1, 2):
    adj = np.clip(b_r5 + delta, 0, 31)
    print(f"  亮帧 R5{delta:+d}: 与暗帧 R5 逐像素相同 {100*(adj==d_r5).mean():.2f}%")

print("\n=== 亮帧的 R5 直方图 (若噪声在 R 最低位, 应该看到双峰) ===")
vals, counts = np.unique((B >> 11) & 0x1F, return_counts=True)
for v, c in sorted(zip(vals, counts), key=lambda t: -t[1])[:10]:
    print(f"  R5={v:2d}: {c:6d} ({100*c/(W*H):5.2f}%)")

print("\n=== 暗帧的 R5 直方图 ===")
vals, counts = np.unique((D >> 11) & 0x1F, return_counts=True)
for v, c in sorted(zip(vals, counts), key=lambda t: -t[1])[:10]:
    print(f"  R5={v:2d}: {c:6d} ({100*c/(W*H):5.2f}%)")

print("\n=== 同一帧内, 相邻像素的 R5 变化分布 (自然图像应该很平滑) ===")
for nm, F in (("亮帧", B), ("暗帧", D)):
    r5 = (F >> 11) & 0x1F
    dh = np.abs(np.diff(r5.astype(int), axis=1))
    vals, counts = np.unique(dh, return_counts=True)
    tot = dh.size
    s = "  ".join(f"Δ{v}:{100*c/tot:.1f}%" for v, c in sorted(zip(vals, counts))[:6])
    print(f"  {nm}: {s}")
