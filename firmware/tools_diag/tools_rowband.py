"""
【关键】两个【同样亮度】的帧之间, 差异集中在哪些行?

已知: 亮帧 vs 亮帧 NCC=0.98, 暗帧 vs 暗帧 NCC=0.955。
      如果采集链路干净 + 曝光固定, 静态场景下应该是 ~0.999。

问: 差异是
  (a) 集中在某一段行  -> 帧是【拼接】的 (传感器行窗口 / VSYNC 错位)
  (b) 均匀分布在所有行 -> 是传感器读出噪声 (模拟端)

这个区分决定修哪里:
  (a) -> 改传感器的窗口/时序 (GC0308 WIN_HEIGHT / 行数), 或改 DVP 行数配置
  (b) -> 降增益/曝光, 或查模拟前端
"""
import numpy as np
import urllib.request, time

W, H = 320, 240
IP = "192.168.2.138"


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.float64)


N = 12
frames = []
for i in range(N):
    frames.append(load(raw()))
    time.sleep(0.2)

lum = np.array([f.mean() for f in frames])
print("各帧亮度: " + " ".join(f"{l:6.0f}" for l in lum))

bright = [i for i in range(N) if lum[i] > np.median(lum)]
dark = [i for i in range(N) if lum[i] <= np.median(lum)]
print(f"亮帧索引={bright}  暗帧索引={dark}")

print()
print("=== 同亮度帧两两之间: 逐行差异的分布 ===")


def analyze(group, name):
    if len(group) < 2:
        print(f"  {name}: 帧数不足")
        return
    for a in range(len(group) - 1):
        i, j = group[a], group[a + 1]
        d = np.abs(frames[i] - frames[j]).mean(axis=1)      # 每行的平均绝对差
        med = np.median(d)
        # 差异最大的连续行段
        thr = max(med * 3, 100)
        hot = d > thr
        # 找连续段
        segs = []
        s = None
        for y in range(H):
            if hot[y] and s is None:
                s = y
            elif not hot[y] and s is not None:
                segs.append((s, y - 1, d[s:y].mean()))
                s = None
        if s is not None:
            segs.append((s, H - 1, d[s:H].mean()))
        print(f"  {name} 帧{i} vs 帧{j}: 行差异 中位={med:6.1f} P90={np.percentile(d,90):6.1f} "
              f"最大={d.max():7.1f}  超阈值行数={hot.sum():3d}")
        if segs:
            print(f"      差异集中的行段: {[(a2,b2,round(c,1)) for a2,b2,c in segs[:8]]}")


analyze(bright, "亮")
analyze(dark, "暗")

print()
print("=== 跨亮度 (亮 vs 暗): 逐行差异, 看是否也有同样的行段结构 ===")
if bright and dark:
    i, j = bright[0], dark[0]
    d = np.abs(frames[i] - frames[j]).mean(axis=1)
    print(f"  亮{i} vs 暗{j}: 中位={np.median(d):6.1f} 最大={d.max():7.1f}")
    print("  逐行差异 (每 8 行): " + " ".join(f"{d[y]:5.0f}" for y in range(0, H, 8)))

print()
print("=== 逐行时间方差: 对【同亮度】的帧, 每行在时间上的波动 ===")
for name, group in (("亮", bright), ("暗", dark)):
    if len(group) < 2:
        continue
    stack = np.array([frames[i] for i in group])
    rowvar = stack.std(axis=0).mean(axis=1)
    print(f"  {name}组 每行时间标准差: 中位={np.median(rowvar):6.1f} "
          f"最大={rowvar.max():6.1f} (行 {int(np.argmax(rowvar))})")
    print("    每 8 行: " + " ".join(f"{rowvar[y]:5.0f}" for y in range(0, H, 8)))
