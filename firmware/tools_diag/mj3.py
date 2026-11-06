import socket, time, sys
import numpy as np
from PIL import Image
from io import BytesIO
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
HOST, PORT = '192.168.2.138', 8080

s = socket.create_connection((HOST, PORT), timeout=10)
s.sendall(b'GET /stream HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\n\r\n' % (HOST.encode(), PORT))
s.settimeout(3)

buf = b''
frames = []
t0 = time.time()
while len(frames) < 6 and time.time() - t0 < 25:
    try:
        d = s.recv(32768)
    except socket.timeout:
        continue
    if not d:
        break
    buf += d
    while True:
        i = buf.find(b'\xff\xd8')
        j = buf.find(b'\xff\xd9')
        if i != -1 and j != -1 and j > i:
            frames.append(buf[i:j+2])
            buf = buf[j+2:]
        else:
            break
s.close()

print('抓到 %d 帧 JPEG' % len(frames))
for i, f in enumerate(frames):
    try:
        im = Image.open(BytesIO(f)).convert('RGB')
    except Exception as e:
        print('  frame%d 解码失败: %s' % (i, e))
        continue
    a = np.asarray(im).astype(float)
    rh = np.abs(np.diff(a, axis=1)).mean()
    rv = np.abs(np.diff(a, axis=0)).mean()
    # 行方差剖面: 花屏的行会有突变
    rowmean = a.mean(axis=(1, 2))
    rowjump = np.abs(np.diff(rowmean)).max()
    print('  JPEG[%d] %s 横向粗糙度=%6.2f 纵向粗糙度=%6.2f 行间最大跳变=%6.2f 字节=%d'
          % (i, im.size, rh, rv, rowjump, len(f)))
    im.save(OUT + '\\mj_%d.png' % i)
