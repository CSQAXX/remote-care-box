"""
【关键验证】抓到的帧是不是【两帧的拼接】?

如果 /raw 抓到的帧其实是"上半帧来自帧A + 下半帧来自帧B", 那么:
  - 每一帧的【行亮度剖面】会有一处明显的断点
  - 相邻两帧之间会存在一个"行偏移"关系 (frame[i+1] 的某一行 == frame[i] 的另一行)

这个测试能一次性区分两种可能:
  (a) 帧是真的花 (数据损坏)
  (b) 帧是拼接/错位 (采集时机问题)
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
for i in range(8):
    frames.append(load(raw()))
    time.sleep(0.2)

print("=== 每帧的行亮度剖面 (每 20 行一个值) ===")
for i, f in enumerate(frames):
    prof = f.mean(axis=1)
    print(f"  帧{i}: " + " ".join(f"{prof[y]:6.0f}" for y in range(0, H, 20)))

print()
print("=== 每帧行亮度剖面的【最大跳变】位置 (拼接会在某行突然变) ===")
for i, f in enumerate(frames):
    prof = f.mean(axis=1)
    d = np.abs(np.diff(prof))
    k = int(np.argmax(d))
    # 排除场景本身的渐变: 看跳变是否远大于邻近的中位跳变
    med = float(np.median(d))
    print(f"  帧{i}: 最大跳变在 行{k}->{k+1} ({d[k]:7.0f}), 邻域中位跳变 {med:6.1f}, "
          f"比值 {d[k]/max(med,1):5.1f}x")

print()
print("=== 相邻帧之间的行偏移匹配 (frame[i+1] 行(y+k) == frame[i] 行(y)?) ===")
for i in range(len(frames) - 1):
    a, b = frames[i], frames[i+1]
    best = None
    for k in range(-40, 41):
        # 比 frame[i][40:200] 与 frame[i+1][40-k:200-k]
        ya, yb = 40, 40 - k
        if ya < 0 or ya + 160 > H or yb < 0 or yb + 160 > H:
            continue
        err = np.abs(a[ya:ya+160].astype(float) - b[yb:yb+160].astype(float)).mean()
        if best is None or err < best[1]:
            best = (k, err)
    err0 = np.abs(a[40:200].astype(float) - b[40:200].astype(float)).mean()
    print(f"  {i}->{i+1}: 无偏移误差 {err0:9.1f}   最佳偏移 {best[0]:+3d} 误差 {best[1]:9.1f} "
          f"(改善 {err0/max(best[1],0.01):5.1f}x)")
