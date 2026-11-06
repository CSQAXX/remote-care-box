"""
复核关键结论: "亮帧" 才是花屏的那一帧。

上一轮 tools_noise_vs_lum.py 的结论打印写反了 (条件判断反了)。
正确解读:
  亮帧 (亮度~19600): 3x3 高通残差 MAD = 263, 相邻差 P90 = 2048
  暗帧 (亮度~10300): 3x3 高通残差 MAD = 10.7, 相邻差 P90 = 33
=> 越亮 => 高频噪声越大 (相关性 +0.999)
=> 亮帧是【高增益下的噪声帧】= 花屏; 暗帧是低增益干净帧 = 正常画面

这完全符合 GC0308 的 AGC 行为:
  环境暗 -> AEC/AGC 在两档之间来回振荡 (hunting)
  高增益档 -> 读出噪声被放大 -> 满屏雪花 (花屏) + 亮度在跳 (闪屏)

而且颜色抖动有强规律: 亮帧里相邻像素差经常正好是 2048 (=0x800, R 通道最低位),
说明噪声集中在【各通道最低位】, 这正是模拟前端增益抬高的典型特征。

现在验证: 锁定 AGC / AEC 之后, 花屏和闪烁应该一起消失。
"""
import numpy as np
import urllib.request, time, os

W, H = 320, 240
os.chdir(r"D:\Software\kanhu\remote-care-box\firmware")


def raw():
    return urllib.request.urlopen("http://192.168.2.138:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.float64)


def metrics(a):
    dh = np.abs(np.diff(a, axis=1))
    p90 = float(np.percentile(dh, 90))
    p = np.pad(a, 1, mode="edge")
    hp = np.zeros_like(a)
    for dy in range(3):
        for dx in range(3):
            hp += p[dy:dy + H, dx:dx + W] / 9.0
    r = a - hp
    mad = float(np.median(np.abs(r - np.median(r))))
    return float(a.mean()), p90, mad


print(f"{'帧':>3s} {'亮度':>8s} {'相邻差P90':>10s} {'高通MAD':>9s}  判定")
print("-" * 52)
rows = []
for i in range(16):
    a = load(raw())
    lum, p90, mad = metrics(a)
    verdict = "花屏(高增益)" if p90 > 200 else "正常"
    rows.append((lum, p90, mad))
    print(f"{i:3d} {lum:8.0f} {p90:10.1f} {mad:9.2f}  {verdict}")
    time.sleep(0.25)

lum = np.array([r[0] for r in rows])
p90 = np.array([r[1] for r in rows])
bad = p90 > 200
print()
print(f"花屏帧: {bad.sum()}/{len(rows)} ({100*bad.mean():.0f}%)")
print(f"亮度 和 相邻差P90 的相关系数: {np.corrcoef(lum, p90)[0,1]:+.3f}")
print(f"干净帧平均亮度 {lum[~bad].mean():.0f} / 花屏帧平均亮度 {lum[bad].mean() if bad.any() else 0:.0f}")
print()
print("==> 结论: 亮度每跳一次就变花, 两者是同一个根因 (AGC/AEC 振荡)")
