"""
【核心验证】暗帧是"噪声爆炸"还是"只是变暗"?

已知现象 (tools_dark.py 实测):
  亮帧 平均亮度 ~19450, 暗帧 平均亮度 ~10300
  亮帧之间互相比: k=0.995, 相对残差中位 0.5%  -> 稳定
  暗帧 vs 亮帧:  k=0.509, 相对残差中位 22%   -> 不只是变暗, 内容本身乱掉了

假设: 传感器 AGC 把增益拉得很高 -> 读出噪声被放大 -> 画面出现满屏雪花(花屏)
     同时 AEC 在两个曝光档之间来回跳 -> 亮度闪烁(闪屏)

验证指标 (空间高频噪声, 对全局增益不敏感):
  noise = 相邻像素差的高分位数 (用 MAD 抗离群)
  亮帧的 noise 应该显著低于暗帧

如果成立, 修复方向就是: 限制 AGC 增益 / 固定曝光 / 提高 AEC 目标亮度。
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


def spatial_noise(a):
    """
    对全局增益不敏感的空间噪声指标:
      1) 高通残差: a - 3x3 均值, 取 MAD
      2) 相邻像素差的 MAD
    用 MAD 而不是 std, 避免被画面里的强边缘带偏。
    """
    # 水平相邻差
    dh = np.diff(a, axis=1)
    mad_h = np.median(np.abs(dh - np.median(dh)))
    # 3x3 高通
    k = np.ones((3, 3)) / 9.0
    pad = np.pad(a, 1, mode="edge")
    hp = np.zeros_like(a)
    for dy in range(3):
        for dx in range(3):
            hp += pad[dy:dy + H, dx:dx + W] * k[dy, dx]
    resid = a - hp
    mad_v = np.median(np.abs(resid - np.median(resid)))
    return mad_h, mad_v


def main():
    s = open_clean(2.5)
    cmd(s, "cbar 0", 1.0)
    time.sleep(3.0)

    frames = []
    for i in range(14):
        try:
            frames.append(load(http_raw()))
        except Exception as e:
            print(f"  抓帧失败 {e}")
        time.sleep(0.3)

    print("=== 逐帧: 亮度 + 空间噪声 ===")
    print(f"{'帧':>3s} {'平均亮度':>9s} {'亮/暗':>6s} {'相邻差MAD':>10s} {'高通残差MAD':>11s}")
    bright_noise, dark_noise = [], []
    for i, f in enumerate(frames):
        mh, mv = spatial_noise(f)
        lum = f.mean()
        is_bright = lum > 15000
        (bright_noise if is_bright else dark_noise).append(mv)
        print(f"{i:3d} {lum:9.0f} {'亮' if is_bright else '暗':>6s} {mh:10.2f} {mv:11.2f}")

    if bright_noise and dark_noise:
        print()
        print(f"  亮帧高通噪声均值: {np.mean(bright_noise):.2f}")
        print(f"  暗帧高通噪声均值: {np.mean(dark_noise):.2f}")
        print(f"  暗/亮 噪声比: {np.mean(dark_noise)/max(np.mean(bright_noise),0.01):.2f}x")
        print()
        if np.mean(dark_noise) > np.mean(bright_noise) * 2:
            print("  ==> 结论: 暗帧的噪声远高于亮帧 => 【AGC 高增益放大读出噪声】")
            print("      这就是 LCD 上的\"花屏\"(满屏雪花), AEC 跳档就是\"闪屏\"。")
        else:
            print("  ==> 结论: 噪声与亮度无关, 不是 AGC 增益问题")

    s.close()
    print("\nALL DONE", flush=True)


if __name__ == "__main__":
    main()
