"""
判定"暗帧"到底是 AE 振荡还是数据损坏。

现象: 抓 8 帧, 平均亮度在 19800 和 10400 之间随机跳。
两种可能:
  (a) 传感器 AE 真的在两个曝光之间振荡 -> 画面是同一场景的亮度缩放
  (b) 帧被污染 (半帧旧数据/半帧新数据) -> 不是缩放, 是结构错乱

判据:
  - 如果暗帧 ≈ 亮帧 * k (整体线性缩放, 逐像素拟合残差很小) -> (a) AE 振荡
  - 如果残差很大 -> (b) 数据损坏

同时对比: LCD 画/不画摄像头 时, 暗帧是否还在。
"""
import numpy as np
import urllib.request, time, serial, os

IP = "192.168.2.138"
W, H = 320, 240
os.chdir(r"D:\Software\kanhu\remote-care-box\firmware")


def open_clean(drain_s=2.0):
    s = serial.Serial("COM3", 115200, timeout=0.2)
    try:
        s.dtr = False; s.rts = False
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < drain_s:
        try: s.readline()
        except Exception: pass
    return s


def cmd(s, c, wait=2.0):
    s.write((c + "\n").encode()); s.flush()
    t0 = time.time()
    while time.time() - t0 < wait:
        try: s.readline()
        except Exception: pass


def http_raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.float64)


def fit_scale(a, b):
    """b 是否 = k*a ? 返回 k 和相对残差"""
    m = (a > 2000) & (a < 60000)
    if m.sum() < 500:
        m = a > 200
    k = (m * a * b).sum() / (m * a * a).sum()
    pred = k * a
    resid = np.abs(b - pred)[m] / np.maximum(b[m], 1.0)
    return k, float(np.median(resid)), float((resid < 0.05).mean())


def main():
    s = open_clean(2.5)
    cmd(s, "cbar 0", 1.0)

    for lcd_mode, label in (("lcd0", "LCD 画摄像头"), ("lcd1", "LCD 不画摄像头")):
        cmd(s, lcd_mode, 1.0)
        time.sleep(3.0)
        frames = []
        for i in range(10):
            try:
                frames.append(load(http_raw()))
            except Exception as e:
                print(f"  抓帧失败 {e}")
            time.sleep(0.3)
        lum = [f.mean() for f in frames]
        print(f"\n=== {label} ===")
        print(f"  各帧平均亮度: {[f'{v:.0f}' for v in lum]}")
        bright = max(frames, key=lambda f: f.mean())
        bl = bright.mean()
        print(f"  以最亮帧 (亮度 {bl:.0f}) 为基准:")
        for i, f in enumerate(frames):
            k, med_res, frac_ok = fit_scale(bright, f)
            tag = "亮" if f.mean() > bl * 0.9 else "暗"
            print(f"    帧{i} ({tag}) 亮度{f.mean():8.0f}  拟合k={k:6.3f}  "
                  f"相对残差中位={med_res*100:5.1f}%  残差<5%的像素={frac_ok*100:5.1f}%")
        # 暗帧是不是"一半新一半旧"?
        print("  逐行亮度 (亮帧 vs 暗帧):")
        dark = min(frames, key=lambda f: f.mean())
        for y in range(0, H, 24):
            rb = bright[y].mean(); rd = dark[y].mean()
            print(f"    行{y:3d}: 亮={rb:7.0f}  暗={rd:7.0f}  比值={rd/max(rb,1):5.2f}")

    s.close()
    print("\nALL DONE", flush=True)


if __name__ == "__main__":
    main()
