"""
【决定性测试】用摄像头拍 LCD 自己画的测试图样。

思路: 摄像头和 LCD 在同一块板子上。让 LCD 画一张【固定的】测试图样
(色条 + 灰阶 + 8x8 棋盘格), 然后用 /raw 抓摄像头拍到的画面。

如果显示链路正常:
  - 抓到的图里应该能看清 16 条竖条 (8 原色 + 8 灰阶)
  - 下半屏的 8x8 棋盘格边缘应该锐利
  - 连续帧应该完全一致 (图样是静态的)
如果显示链路有问题 (SPI 采样错/缓冲复用竞争):
  - 竖条会出现错位、串色
  - 棋盘格会糊掉
  - 连续帧会跳变

这是唯一一个能【客观】判定显示链路好坏的闭环测试。
"""
import numpy as np
import urllib.request, time, serial, os
from PIL import Image

W, H = 320, 240
IP = "192.168.2.138"
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


def cmd(s, c, wait=3.0):
    s.write((c + "\n").encode()); s.flush()
    t0 = time.time()
    while time.time() - t0 < wait:
        try: s.readline()
        except Exception: pass


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int64)


def to_img(a):
    r = ((a >> 11) & 0x1F) * 255 // 31
    g = ((a >> 5) & 0x3F) * 255 // 63
    b = (a & 0x1F) * 255 // 31
    return np.dstack([r, g, b]).astype(np.uint8)


def analyze(a, tag, idx):
    lum = a.mean(axis=1)   # 每行平均亮度
    Image.fromarray(to_img(a)).resize((W*2, H*2), Image.NEAREST).save(f"lcd_{tag}_{idx}.png")

    # 上下半屏的平均亮度 (上半屏=竖条, 下半屏=棋盘格)
    top = lum[:H//2].mean()
    bot = lum[H//2:].mean()
    # 棋盘格: 相邻像素差应该很大 (黑白交替 8x8)
    botpix = a[H//2:, :].astype(float)
    botdh = np.abs(np.diff(botpix, axis=1))
    # 竖条: 横向应该有 16 个台阶 (16 条带), 算一行的过零点
    row = a[H//4, :].astype(float)
    dh = np.abs(np.diff(row))
    edges = int((dh > dh.max() * 0.3).sum())
    return top, bot, float(np.percentile(botdh, 90)), edges


def main():
    s = open_clean(2.5)
    cmd(s, "lcd1", 2.0)   # 画固定测试图样
    time.sleep(3.0)

    print("=== 抓 8 帧摄像头拍到的 LCD 测试图样 ===")
    frames = []
    for i in range(8):
        a = load(raw())
        frames.append(a)
        top, bot, botdh, edges = analyze(a, "pat", i)
        print(f"  帧{i}: 上半屏亮度={top:7.0f} 下半屏={bot:7.0f} "
              f"棋盘相邻差P90={botdh:8.1f} 竖条边缘数={edges}")
        time.sleep(0.4)

    print()
    print("=== 帧间一致性 (测试图样是静态的, 差异应该很小) ===")
    for i in range(len(frames) - 1):
        x = frames[i] ^ frames[i + 1]
        d = sum(((x >> k) & 1).sum() for k in range(16)) / 16 / (W * H) * 100
        ad = np.abs(frames[i] - frames[i + 1]).mean()
        print(f"  {i}->{i+1}: bit翻转 {d:6.2f}%  平均差 {ad:8.1f}")

    print()
    print("=== 竖条结构检查 (上半屏应该能数出 16 条带) ===")
    a = frames[0]
    row = a[H//4, :].astype(float)
    dh = np.abs(np.diff(row))
    thr = dh.max() * 0.25
    idxs = [x for x in range(1, W) if dh[x-1] > thr]
    print(f"  边缘位置: {idxs[:30]}")
    print(f"  条带数 (约): {len(idxs)}  (期望 ~15 条边界)")
    print("  各条带的取样颜色 (每 20 像素一段):")
    for x0 in range(0, W, 20):
        seg = a[H//4, x0:x0+20].reshape(-1, 3)
        m = np.median(seg, axis=0)
        r, g, b = int(m[0]), int(m[1]), int(m[2])
        print(f"    x{x0:3d}-{x0+19:3d}: R={r:2d} G={g:2d} B={b:2d}")

    cmd(s, "lcd0", 1.5)
    s.close()
    print("\nALL DONE")


if __name__ == "__main__":
    main()
