"""彩条 PNG 验证: 确认 cbar 1 真的打开了, 并量化逐帧噪声"""
import serial, time
import numpy as np
import urllib.request, os
from PIL import Image

W, H = 320, 240
IP = "192.168.2.138"
PORT = "COM3"
out = r"D:\Software\kanhu\remote-care-box\firmware\out"
os.makedirs(out, exist_ok=True)


def send(cmd, wait=1.5):
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


def to_rgb(a):
    v = np.clip(a, 0, 65535).astype(np.uint32)
    r = ((v >> 11) & 0x1F) * 255 // 31
    g = ((v >> 5) & 0x3F) * 255 // 63
    b = (v & 0x1F) * 255 // 31
    return np.dstack([r, g, b]).astype(np.uint8)


def ncc(a, b):
    a = a.astype(np.float64) - a.mean()
    b = b.astype(np.float64) - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 0 else 0


def collect(n, tag):
    frames = []
    for i in range(n):
        frames.append(load(raw()))
        time.sleep(0.2)
    lum = [f.mean() for f in frames]
    print(f"  亮度: " + " ".join(f"{l:6.0f}" for l in lum))
    # 逐帧 NCC vs 帧0
    print("  NCC vs 帧0: " + " ".join(f"{ncc(frames[0], f):+.3f}" for f in frames))
    # 每帧内部的高频噪声: 水平相邻差 P90
    for i, f in enumerate(frames):
        d = np.abs(np.diff(f.astype(np.int64), axis=1))
        print(f"    帧{i}: hf P50={np.percentile(d,50):5.0f} P90={np.percentile(d,90):5.0f} "
              f"P99={np.percentile(d,99):5.0f} max={d.max():5d}")
    Image.fromarray(to_rgb(frames[0])).save(os.path.join(out, f"{tag}_f0.png"))
    Image.fromarray(to_rgb(frames[min(1, len(frames)-1)])).save(os.path.join(out, f"{tag}_f1.png"))
    # 帧0 与帧1 的差值放大
    diff = np.abs(frames[0] - frames[min(1, len(frames)-1)])
    Image.fromarray(to_rgb(diff * 8)).save(os.path.join(out, f"{tag}_diff.png"))
    return frames


print("### 真实场景 ###")
collect(6, "scene")

print()
print("### cbar 1 ###")
send("cbar 1", 4)
collect(8, "cbar")

print()
print("### cbar 0 ###")
send("cbar 0", 2)
