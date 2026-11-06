"""地面真相: 直接分析浏览器 MJPEG 流里连续帧的亮度序列。

如果用户看到的"闪烁"是 AE 在跳, 那么流里的连续帧亮度会明显起伏。
"""
import socket, time, re, numpy as np
from PIL import Image
import io

HOST, PORT = "192.168.2.138", 8080

sk = socket.create_connection((HOST, PORT), timeout=10)
sk.sendall(f"GET /stream HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
buf = bytearray()
t0 = time.time()
while len(buf) < 900000 and time.time() - t0 < 25:
    try:
        c = sk.recv(65536)
        if not c:
            break
        buf += c
    except socket.timeout:
        break
sk.close()

body = bytes(buf).split(b"\r\n\r\n", 1)[-1]
# 切出所有 JPEG 帧
frames = []
i = 0
while True:
    a = body.find(b"\xff\xd8\xff", i)
    if a < 0:
        break
    b = body.find(b"\xff\xd9", a)
    if b < 0:
        break
    frames.append(body[a:b+2])
    i = b + 2

print(f"流里共 {len(frames)} 帧 JPEG")
means = []
for n, f in enumerate(frames):
    im = Image.open(io.BytesIO(f)).convert("L")
    means.append(float(np.asarray(im).mean()))

print("\n连续帧亮度 (前 40 帧):")
for k in range(0, min(40, len(means)), 10):
    print("   " + "  ".join(f"{m:6.1f}" for m in means[k:k+10]))

if len(means) > 3:
    arr = np.array(means)
    d = np.abs(np.diff(arr))
    print(f"\n亮度: min={arr.min():.1f} max={arr.max():.1f} "
          f"平均={arr.mean():.1f} 标准差={arr.std():.1f}")
    print(f"相邻帧亮度变化: 平均 {d.mean():.1f} 最大 {d.max():.1f} "
          f"({100*d.max()/arr.mean():.1f}%)")
    # 明暗交替检测
    alt = sum(1 for k in range(len(d)-1)
              if (arr[k+1]-arr[k]) * (arr[k+2]-arr[k+1]) < 0)
    print(f"方向交替的帧对: {alt}/{len(d)-1}")
