"""
【决定性实验】传感器内置彩条 (cbar) —— 亮暗交替是"光学/模拟"还是"数字通路"?

原理:
  彩条由 GC0308 内部生成, 固定图案, 不经过镜头和光电转换。
  如果打开彩条后仍然出现"亮帧/暗帧"交替 -> 问题在【数字通路】
  (DVP / 转换器 / PSRAM DMA / 帧缓冲)
  如果彩条下完全稳定 -> 问题在【模拟前端】(传感器还在自动调节)
"""
import serial, time, sys
import numpy as np
import urllib.request

W, H = 320, 240
IP = "192.168.2.138"
PORT = "COM3"


def send(cmd, wait=0.4):
    s = serial.Serial(PORT, 115200, timeout=0.3)
    s.reset_input_buffer()
    s.write((cmd + "\n").encode())
    s.flush()
    time.sleep(wait)
    s.close()


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int64)


def bitflip(a, b):
    """逐位翻转率"""
    x = np.bitwise_xor(a, b)
    return float((x != 0).sum()) / x.size


def run(label, n=10):
    frames = []
    for i in range(n):
        frames.append(load(raw()))
        time.sleep(0.2)
    lum = [f.mean() for f in frames]
    flips = [bitflip(frames[0], f) for f in frames]
    print(f"--- {label} ---")
    print("  亮度: " + " ".join(f"{l:6.0f}" for l in lum))
    print(f"  相对首帧的位翻转率: " + " ".join(f"{f*100:5.2f}%" for f in flips))
    lo = min(lum)
    hi = max(lum)
    print(f"  亮度范围 {lo:.0f}..{hi:.0f}  (比值 {hi/max(lo,1):.2f}x)")
    # 亮暗分组
    med = np.median(lum)
    bright = [i for i in range(n) if lum[i] > med]
    dark = [i for i in range(n) if lum[i] <= med]
    print(f"  亮帧={bright} 暗帧={dark}")
    return frames, lum


print("### 1) 真实场景 (当前状态) ###")
run("真实场景")

print()
print("### 2) 打开传感器内置彩条 ###")
send("cbar 1")
time.sleep(3)
run("彩条", 10)

print()
print("### 3) 关掉彩条 ###")
send("cbar 0")
time.sleep(2)
