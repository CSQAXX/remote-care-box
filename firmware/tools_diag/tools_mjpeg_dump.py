"""直接抓 MJPEG 流里的 JPEG 帧, 存成文件, 用来肉眼判断摄像头数据是否干净。"""
import socket, time, os, sys

HOST, PORT = "192.168.2.138", 8080
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mjpeg_dump")
os.makedirs(OUT, exist_ok=True)

sk = socket.create_connection((HOST, PORT), timeout=10)
sk.sendall(f"GET /stream HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())

buf = bytearray()
t0 = time.time()
while time.time() - t0 < 12:
    try:
        c = sk.recv(65536)
        if not c:
            break
        buf += c
    except socket.timeout:
        break
sk.close()

data = bytes(buf)
print(f"共收到 {len(data)} 字节")

# 找 JPEG SOI/EOI
frames = []
pos = 0
while True:
    s = data.find(b"\xff\xd8", pos)
    if s < 0:
        break
    e = data.find(b"\xff\xd9", s)
    if e < 0:
        break
    frames.append(data[s:e + 2])
    pos = e + 2

print(f"找到 {len(frames)} 帧 JPEG")
for i, f in enumerate(frames):
    p = os.path.join(OUT, f"frame_{i:03d}.jpg")
    with open(p, "wb") as fp:
        fp.write(f)
print(f"已保存到 {OUT}")

# 简单量化: 相邻帧差异 + 每帧大小
if len(frames) >= 2:
    print("\n每帧大小:")
    for i, f in enumerate(frames[:20]):
        print(f"  frame {i:3d}: {len(f)} 字节")
