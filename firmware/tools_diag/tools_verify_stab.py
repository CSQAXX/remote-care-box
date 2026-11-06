"""验证亮度稳定器的效果 (通过 MJPEG 流 —— 这是稳定之后的输出)。

rawdump 是【稳定之前】的原始数据(用来诊断 AE), 所以验证效果必须看流。
"""
import socket, time, re, numpy as np
from PIL import Image
import io

HOST, PORT = "192.168.2.138", 8080


def grab(seconds=30):
    sk = socket.create_connection((HOST, PORT), timeout=10)
    sk.sendall(f"GET /stream HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            c = sk.recv(65536)
            if not c:
                break
            buf += c
        except socket.timeout:
            break
    sk.close()
    body = bytes(buf).split(b"\r\n\r\n", 1)[-1]
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
    return frames


def luma(frames):
    out = []
    for f in frames:
        im = Image.open(io.BytesIO(f)).convert("L")
        out.append(float(np.asarray(im).mean()))
    return np.array(out)


print("=== 流里的连续帧亮度 (稳定器应已生效) ===", flush=True)
fs = grab(35)
print(f"共 {len(fs)} 帧")
a = luma(fs)
for k in range(0, min(len(a), 60), 12):
    print("   " + "  ".join(f"{v:6.1f}" for v in a[k:k+12]))
if len(a) > 3:
    d = np.abs(np.diff(a))
    print(f"\n  min={a.min():.1f} max={a.max():.1f} 均值={a.mean():.1f} 标准差={a.std():.1f}")
    print(f"  相邻帧亮度变化: 平均 {d.mean():.1f} 最大 {d.max():.1f} "
          f"({100*d.max()/a.mean():.1f}%)")
    rng = (a.max()-a.min())/a.min()*100
    print(f"  整体幅度 {rng:.1f}%   {'OK (稳定!)' if rng < 12 else 'FAIL (还在跳)'}")
