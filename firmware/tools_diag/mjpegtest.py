import urllib.request, time, sys, re
import numpy as np
from PIL import Image
from io import BytesIO
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'


def fetch_jpeg(n=3):
    """GET /stream (mjpeg) and pull n complete JPEG frames"""
    frames = []
    req = urllib.request.Request(BASE + '/stream?capture=1')
    resp = urllib.request.urlopen(req, timeout=30)
    buf = b''
    t0 = time.time()
    while len(frames) < n and time.time() - t0 < 30:
        d = resp.read(8192)
        if not d:
            break
        buf += d
        while True:
            i = buf.find(b'\xff\xd8')
            j = buf.find(b'\xff\xd9')
            if i != -1 and j != -1 and j > i:
                frames.append(buf[i:j+2])
                buf = buf[j+2:]
                break
            else:
                break
    resp.close()
    return frames


print('=== 抓 MJPEG 流里的完整 JPEG 帧 ===', flush=True)
fs = fetch_jpeg(4)
print('  抓到 %d 帧' % len(fs))
if not fs:
    print('  (没抓到, 换 /stream?capture 之外的路径)')
    import os
    for p in ('/stream', '/video', '/mjpeg'):
        try:
            r = urllib.request.urlopen(BASE + p, timeout=5)
            print('  %s -> %s' % (p, r.headers.get('Content-Type')))
            r.close()
        except Exception as e:
            print('  %s -> %s' % (p, e))

for i, f in enumerate(fs):
    im = Image.open(BytesIO(f)).convert('RGB')
    a = np.asarray(im).astype(float)
    # horizontal roughness = mean abs diff between adjacent pixels per channel
    rh = np.abs(np.diff(a, axis=1)).mean()
    rv = np.abs(np.diff(a, axis=0)).mean()
    print('  JPEG[%d] size=%s  横向粗糙度=%6.2f  纵向粗糙度=%6.2f  字节=%d'
          % (i, im.size, rh, rv, len(f)))
    im.save(OUT + '\\mjpeg_%d.png' % i)

print()
print('对照: 原始 RGB565 (HTTP /raw) 复算的原始字节差 hi=9~46, lo=24~72')
print('若 JPEG 的横向粗糙度也在 5~40 量级 -> 数据本身就脏 (问题在采集)')
print('若 JPEG 粗糙度 < 3 -> JPEG 路径干净, 数据其实没问题, 问题在 LCD 画屏路径')
