"""
【关键判别】亮帧和暗帧是"同样的内容, 只是增益不同" 还是 "内容真的不同"?

方法: 逐行算【归一化互相关 (NCC)】。
  NCC 对亮度缩放不敏感 (相关系数), 所以:
    - 如果暗帧的每一行在亮帧里都能找到 NCC~1.0 的对应行
      -> 内容完全相同, 差别只是【增益/曝光】 -> 传感器侧问题
    - 如果 NCC 低
      -> 内容真的不同 -> 帧里混了不同时间的行 -> 数据通路问题

这一步把"光学"和"数字"彻底分开。
"""
import numpy as np
import urllib.request, time

W, H = 320, 240
IP = "192.168.2.138"


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.float64)


def ncc(a, b):
    """零均值归一化互相关"""
    a = a - a.mean()
    b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 0 else 0.0


N = 8
frames = [load(raw()) for _ in range(N)]
time.sleep(0.15)

lumas = [f.mean() for f in frames]
print("各帧亮度:", [f"{l:.0f}" for l in lumas])

bright = [i for i in range(N) if lumas[i] > np.median(lumas)]
dark = [i for i in range(N) if lumas[i] <= np.median(lumas)]
print(f"亮帧={bright} 暗帧={dark}")

print()
print("=== 整帧 NCC (亮 vs 暗) ===")
for b in bright[:3]:
    for d in dark[:3]:
        print(f"  亮{b} vs 暗{d}: NCC={ncc(frames[b].ravel(), frames[d].ravel()):+.4f}")
print("=== 整帧 NCC (亮 vs 亮, 暗 vs 暗) ===")
for i in range(min(2, len(bright))):
    for j in range(i + 1, min(4, len(bright))):
        print(f"  亮{bright[i]} vs 亮{bright[j]}: NCC={ncc(frames[bright[i]].ravel(), frames[bright[j]].ravel()):+.4f}")
for i in range(min(2, len(dark))):
    for j in range(i + 1, min(4, len(dark))):
        print(f"  暗{dark[i]} vs 暗{dark[j]}: NCC={ncc(frames[dark[i]].ravel(), frames[dark[j]].ravel()):+.4f}")

print()
print("=== 逐行 NCC: 亮帧的每一行 vs 暗帧的每一行 (看能不能对上) ===")
if bright and dark:
    b, d = frames[bright[0]], frames[dark[0]]
    row_ncc = np.zeros((H, H))
    for y1 in range(H):
        r1 = b[y1]
        for y2 in range(H):
            row_ncc[y1, y2] = ncc(r1, d[y2])
    # 对每个亮帧行, 找出暗帧中 NCC 最高且【同一行号】的位置
    diag = np.array([row_ncc[y, y] for y in range(H)])
    best_any = row_ncc.max(axis=1)
    best_same = diag
    best_off = np.array([np.argmax(np.delete(row_ncc[y], y)) + (1 if np.argmax(np.delete(row_ncc[y], y)) >= y else 0)
                          for y in range(H)])
    print(f"  同行 NCC:  中位={np.median(diag):.4f} 最小={diag.min():.4f} 最大={diag.max():.4f}")
    print(f"  最佳匹配 NCC(不限定行): 中位={np.median(best_any):.4f}")
    same_hit = sum(1 for y in range(H) if abs(best_off[y] - y) <= 2)
    print(f"  最佳匹配落在同一行(+-2)内的行数: {same_hit}/{H}")
    bad = [(y, round(diag[y], 3), best_off[y], round(best_any[y], 3)) for y in range(H) if diag[y] < 0.8]
    print(f"  同行 NCC < 0.8 的行: {len(bad)}  前10: {bad[:10]}")

print()
print("=== 行亮度曲线: 亮帧 vs 暗帧 (归一化到各自最大值) ===")
for i in range(N):
    prof = frames[i].mean(axis=1)
    norm = prof / prof.max()
    print(f"  帧{i}({lumas[i]:.0f}): " + " ".join(f"{norm[y]:.3f}" for y in range(0, H, 16)))
