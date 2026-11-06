import socket, time, sys, urllib.request
import numpy as np
from PIL import Image
from io import BytesIO
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
HOST, PORT = '192.168.2.138', 8080


def jpeg_frames(n=6):
    s = socket.create_connection((HOST, PORT), timeout=10)
    s.sendall(b'GET /stream HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n')
    s.settimeout(3)
    buf = b''
    frames = []
    t0 = time.time()
    while len(frames) < n and time.time() - t0 < 25:
        try:
            d = s.recv(32768)
        except socket.timeout:
            continue
        if not d:
            break
        buf += d
        while True:
            i = buf.find(b'\xff\xd8'); j = buf.find(b'\xff\xd9')
            if i != -1 and j != -1 and j > i:
                frames.append(buf[i:j+2]); buf = buf[j+2:]
            else:
                break
    s.close()
    return frames


print('=== JPEG 帧的亮度统计 (判断房间是否太暗) ===')
for i, f in enumerate(jpeg_frames(6)):
    im = Image.open(BytesIO(f)).convert('L')
    a = np.asarray(im).astype(float)
    print('  JPEG[%d] 均值=%6.2f  中位数=%6.2f  P1=%3d P99=%3d  标准差=%6.2f  饱和像素(>=250)=%5.2f%%'
          % (i, a.mean(), np.median(a), np.percentile(a, 1), np.percentile(a, 99),
             a.std(), (a >= 250).mean() * 100))

print()
print('=== 原始 RGB565 帧的亮度统计 (LCD 看到的) ===')
for i in range(4):
    d = urllib.request.urlopen('http://%s:%d/raw?capture' % (HOST, PORT), timeout=20).read()
    f = np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)
    # 高字节近似亮度 (R5G3)
    lum = ((f[:, :, 0] >> 3) * 2 + (f[:, :, 0] & 7)).astype(float)
    print('  RAW[%d] 亮度均值=%6.2f  中位数=%6.2f  标准差=%6.2f  最暗=%3d 最亮=%3d'
          % (i, lum.mean(), np.median(lum), lum.std(), lum.min(), lum.max()))

print()
print('解读:')
print('  均值 < 30  -> 房间很暗, AGC 把增益拉满 -> 传感器噪声被放大成"雪花"')
print('  均值 > 60  -> 光线正常, 那噪声就是链路问题')
