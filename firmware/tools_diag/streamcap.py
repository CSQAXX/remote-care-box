import urllib.request, time, numpy as np
from PIL import Image
import io

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'

# 1) capture MJPEG stream frames
print('=== capturing MJPEG stream (3 frames) ===')
r = urllib.request.urlopen(BASE + '/stream', timeout=20)
buf = b''
frames = []
t0 = time.time()
while time.time() - t0 < 8 and len(frames) < 3:
    d = r.read(4096)
    if not d:
        break
    buf += d
    while True:
        s = buf.find(b'\xff\xd8')
        e = buf.find(b'\xff\xd9')
        if s != -1 and e != -1 and e > s:
            frames.append(buf[s:e + 2])
            buf = buf[e + 2:]
            break
        else:
            break
r.close()
print('  got %d JPEG frames' % len(frames))
for i, f in enumerate(frames):
    open(OUT + '\\stream_%d.jpg' % i, 'wb').write(f)
    im = Image.open(io.BytesIO(f)).convert('RGB')
    a = np.asarray(im).astype(float)
    lap = np.abs(a[1:-1,1:-1]*4 - a[:-2,1:-1] - a[2:,1:-1] - a[1:-1,:-2] - a[1:-1,2:]).mean()
    print('  frame%d %dx%d  laplacianHF=%.2f  size=%d' % (i, im.width, im.height, lap, len(f)))
    im.save(OUT + '\\stream_%d.png' % i)
