import urllib.request, time, sys
import numpy as np
from PIL import Image
from io import BytesIO
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'

req = urllib.request.Request(BASE + '/stream')
req.add_header('Accept', 'multipart/x-mixed-replace')
resp = urllib.request.urlopen(req, timeout=30)
print('Content-Type:', resp.headers.get('Content-Type'))

frames = []
buf = b''
t0 = time.time()
while len(frames) < 5 and time.time() - t0 < 40:
    d = resp.read(16384)
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
        if i == -1 and len(buf) > 4096:
            buf = buf[-1024:]
resp.close()

print('抓到 %d 帧 JPEG' % len(frames))
for i, f in enumerate(frames):
    try:
        im = Image.open(BytesIO(f)).convert('RGB')
    except Exception as e:
        print('  frame%d 解码失败: %s (len=%d)' % (i, e, len(f)))
        continue
    a = np.asarray(im).astype(float)
    rh = np.abs(np.diff(a, axis=1)).mean()
    rv = np.abs(np.diff(a, axis=0)).mean()
    print('  JPEG[%d] %s 横向粗糙度=%6.2f 纵向粗糙度=%6.2f 字节=%d'
          % (i, im.size, rh, rv, len(f)))
    im.save(OUT + '\\mjpeg_%d.png' % i)
