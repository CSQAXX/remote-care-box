"""
【决定性】暗帧是不是【亮帧的另一种纵向采样/缩放】?

背景:
  亮帧 vs 暗帧 NCC 只有 0.62, 而逐行亮度比 k 从 1.56 (顶) 变到 2.9 (中)
  再回到 1.76 (底) —— k 不是常数, 说明不是纯增益差。

新假设 H6: 传感器输出的【行数/行相位】在两帧之间不同, 导致
             同一场景被纵向拉伸/压缩后存进固定 240 行的缓冲,
             于是画面结构变了 (NCC 掉下来), 亮度也跟着变。

验证方法 (带增益归一化, 排除纯亮度差):
  对暗帧做纵向重采样 (缩放因子 s, 亚像素偏移), 每次都拟合一个全局增益 k,
  再和亮帧算 NCC。
  如果某个 s + offset 让 NCC 跳到 >0.95 -> H6 成立, 是传感器行时序/窗口问题。

也顺便试横向缩放 (H7) 和 180 度旋转 (H8)。
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
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.float64)


def ncc(a, b):
    a = a.astype(np.float64).ravel() - a.mean()
    b = b.astype(np.float64).ravel() - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 0 else 0.0


def fit_ncc(A, B):
    k = float((A.ravel() * B.ravel()).sum() / max((B.ravel() ** 2).sum(), 1e-9))
    return ncc(A, B * k), k


N = 14
frames = []
for i in range(N):
    frames.append(load(raw()))
    time.sleep(0.15)

lum = np.array([f.mean() for f in frames])
bi, di = int(np.argmax(lum)), int(np.argmin(lum))
B, D = frames[bi], frames[di]
print(f"亮帧#{bi} luma={lum[bi]:.0f}   暗帧#{di} luma={lum[di]:.0f}")

n0, k0 = fit_ncc(B, D)
print(f"\n基线 (无变换): NCC={n0:+.4f}  k={k0:.3f}")

print("\n=== H6: 暗帧纵向缩放 (每种缩放都重新拟合增益) ===")
best = (n0, 1.0, 0)
for s in np.arange(0.80, 1.25, 0.01):
    nh = int(round(H * s))
    if nh < 8 or nh > 400:
        continue
    src = np.linspace(0, H - 1, nh)
    D2 = np.empty((nh, W))
    for x in range(W):
        D2[:, x] = np.interp(src, np.arange(H), D[:, x])
    back = np.linspace(0, nh - 1, H)
    D3 = np.empty((H, W))
    for x in range(W):
        D3[:, x] = np.interp(back, np.arange(nh), D2[:, x])
    n, k = fit_ncc(B, D3)
    if n > best[0]:
        best = (n, s, k)
print(f"  最佳: 缩放 s={best[1]:.2f}  NCC={best[0]:+.4f}  k={best[2]:.3f}")

print("\n=== H6b: 暗帧纵向亚像素平移 ===")
best = (n0, 0.0)
for sh in np.arange(-30, 30, 0.25):
    src = np.clip(np.arange(H) - sh, 0, H - 1)
    i0 = np.floor(src).astype(int)
    i1 = np.clip(i0 + 1, 0, H - 1)
    fr = (src - i0)[:, None]
    D3 = D[i0] * (1 - fr) + D[i1] * fr
    n, k = fit_ncc(B, D3)
    if n > best[0]:
        best = (n, sh)
print(f"  最佳: 平移 {best[1]:+.2f} 行  NCC={best[0]:+.4f}")

print("\n=== H6c: 亮帧纵向缩放 (反方向) ===")
best = (n0, 1.0)
for s in np.arange(0.80, 1.25, 0.01):
    nh = int(round(H * s))
    src = np.linspace(0, H - 1, nh)
    B2 = np.empty((nh, W))
    for x in range(W):
        B2[:, x] = np.interp(src, np.arange(H), B[:, x])
    back = np.linspace(0, nh - 1, H)
    B3 = np.empty((H, W))
    for x in range(W):
        B3[:, x] = np.interp(back, np.arange(nh), B2[:, x])
    n, k = fit_ncc(D, B3)
    if n > best[0]:
        best = (n, s)
print(f"  最佳: 缩放 s={best[1]:.2f}  NCC={best[0]:+.4f}")

print("\n=== H7: 暗帧横向缩放 ===")
best = (n0, 1.0)
for s in np.arange(0.80, 1.25, 0.02):
    nw = int(round(W * s))
    src = np.linspace(0, W - 1, nw)
    D2 = np.empty((H, nw))
    for y in range(H):
        D2[y] = np.interp(src, np.arange(W), D[y])
    back = np.linspace(0, nw - 1, W)
    D3 = np.empty((H, W))
    for y in range(H):
        D3[y] = np.interp(back, np.arange(nw), D2[y])
    n, k = fit_ncc(B, D3)
    if n > best[0]:
        best = (n, s)
print(f"  最佳: 缩放 s={best[1]:.2f}  NCC={best[0]:+.4f}")

print("\n=== H8: 旋转/翻转 ===")
for name, arr in [("180度", D[::-1, ::-1]), ("上下翻", D[::-1, :]), ("左右翻", D[:, ::-1])]:
    n, k = fit_ncc(B, arr)
    print(f"  {name}: NCC={n:+.4f} k={k:.3f}")

print("\n=== 结论 ===")
if best[0] > 0.9:
    print("  存在能解释两帧差异的【几何变换】 -> 传感器行/列时序或窗口配置问题")
else:
    print("  没有几何变换能解释 -> 两帧内容差异是【模拟/光学】层面的 (曝光/增益真的在变)")
