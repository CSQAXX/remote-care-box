"""把流里的帧存成 PNG, 供肉眼检查。"""
import socket, time, numpy as np
from PIL import Image
import io

HOST, PORT = "192.168.2.138", 8080
DEV = r"D:\Software\kanhu\remote-care-box\firmware"

sk = socket.create_connection((HOST, PORT), timeout=10)
sk.sendall(f"GET /stream HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
buf = bytearray()
t0 = time.time()
while time.time() - t0 < 22:
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

# 均匀取 6 帧, 拼成一张对比图
picks = np.linspace(0, len(frames) - 1, 6).astype(int)
ims = []
for k in picks:
    im = Image.open(io.BytesIO(frames[k])).convert("RGB")
    ims.append(im)
    im.save(f"{DEV}\\stream_{k}.png")
    print(f"  stream_{k}.png  亮度 {np.asarray(im.convert('L')).mean():.1f}")

w, h = ims[0].size
sheet = Image.new("RGB", (w * 3, h * 2))
for idx, im in enumerate(ims):
    sheet.paste(im, ((idx % 3) * w, (idx // 3) * h))
sheet.save(f"{DEV}\\stream_sheet.png")
print(f"  stream_sheet.png ({sheet.size})")
