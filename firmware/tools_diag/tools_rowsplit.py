"""
【决定性测试】帧是不是被【按行混拼】了?

背景:
  固件里 AE 已经是手动的 (agc_auto=false, exp=480, gain=20),
  理论上相邻帧的亮度应该几乎一样。但实测相邻帧亮度差 2 倍,
  而且两帧的【亮度剖面形状不同】(亮帧中间最亮, 暗帧单调递增)
  —— 这不是曝光缩放能产生的, 说明两帧【内容真的不一样】。

假设 H: DVP DMA 正在往帧缓冲里写新一帧的同时, CPU 已经拿到了这个缓冲,
       于是读到的是 "上半是旧帧 + 下半是新帧" 的混合体。

测试方法:
  连抓 N 帧。对每一帧 f 的每一行 y, 找出"哪一帧 g 的第 y 行与它最像"。
  如果 f 的第 y 行其实来自 g (g != f), 那么 f 的行就会跨帧跳变。

输出:
  - 每帧的"最佳同源行"分布: 同一帧占比 vs 其他帧占比
  - 每帧相对于【时间相邻帧】的行级差异曲线: 如果出现"某些行特别不同",
    说明那些行来自别的时间点
"""
import numpy as np
import urllib.request, time, os

W, H = 320, 240
IP = "192.168.2.138"


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.float64)


N = 10
frames = []
for i in range(N):
    frames.append(load(raw()))
    time.sleep(0.15)

frames = np.array(frames)   # (N, H, W)

print("=== 各帧平均亮度 ===")
for i in range(N):
    print(f"  帧{i}: {frames[i].mean():9.0f}")

print()
print("=== 行级来源追踪: 对每帧每行, 找最像哪一帧的同一行 ===")
for i in range(N):
    # (N, H, W) -> 行级距离矩阵 (N, H)
    d = np.abs(frames - frames[i][None]).mean(axis=2)   # (N, H)
    owner = d.argmin(axis=0)     # 每行的最佳来源帧
    counts = np.bincount(owner, minlength=N)
    own = counts[i]
    print(f"  帧{i}: 本帧行数={own:3d}/{H}  其他帧贡献={H-own:3d}  "
          f"分布={dict((k, int(v)) for k, v in enumerate(counts) if v > 0)}")

print()
print("=== 每帧逐行 与【上一帧】同行 的差异 (找突变行) ===")
for i in range(1, N):
    rowdiff = np.abs(frames[i] - frames[i-1]).mean(axis=1)   # (H,)
    med = np.median(rowdiff)
    spikes = [(y, rowdiff[y]) for y in range(H) if rowdiff[y] > med * 3]
    print(f"  {i-1}->{i}: 行差异中位={med:7.0f} 最大={rowdiff.max():7.0f} "
          f"突变行数={len(spikes)}  前5={[(y, int(v)) for y, v in spikes[:5]]}")

print()
print("=== 【关键】帧内水平行 vs 全帧: 找撕裂带 ===")
for i in range(N):
    f = frames[i]
    # 相邻行之间的差异: 若某处撕裂, 那里会有巨大跳变
    d = np.abs(np.diff(f, axis=0)).mean(axis=1)
    med = np.median(d)
    big = [(y + 1, int(d[y])) for y in range(len(d)) if d[y] > med * 4]
    print(f"  帧{i}: 行间跳变中位={med:7.0f} 最大={d.max():7.0f} 撕裂带={big[:6]}")
