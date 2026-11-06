import serial, time, re, sys, urllib.request
import numpy as np
from PIL import Image
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'


def cmd(c, wait=3.0):
    s = serial.Serial('COM3', 115200, timeout=0.2)
    s.reset_input_buffer()
    s.write((c + '\r\n').encode())
    s.flush()
    t0 = time.time()
    buf = b''
    while time.time() - t0 < wait:
        d = s.read(8192)
        if d:
            buf += d
    s.close()
    return re.sub(r'\x1b\[[0-9;]*m', '', buf.decode('utf-8', 'replace'))


def grab(tag, n=3):
    outs = []
    for i in range(n):
        d = urllib.request.urlopen(BASE + '/raw?capture', timeout=20).read()
        b = np.frombuffer(d[:153600], dtype=np.uint8)
        outs.append(b)
        open(OUT + '\\%s_%d.bin' % (tag, i), 'wb').write(b.tobytes())
    return outs


def rgb565(b):
    f = b.reshape(240, 320, 2).astype(np.int64)
    A = f[:, :, 0]; B = f[:, :, 1]
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    return np.dstack([((R5) << 3).astype(np.uint8),
                      ((G3 << 3) | G2).astype(np.uint8),
                      ((B5) << 3).astype(np.uint8)])


def report(rgb, tag):
    def c(x, y):
        x = x.astype(float).ravel(); y = y.astype(float).ravel()
        x -= x.mean(); y -= y.mean()
        d = np.sqrt((x * x).sum() * (y * y).sum())
        return float((x * y).sum() / d) if d > 1e-9 else 0.0
    R, G, B = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    # colorbar has vertical stripes -> row-to-row correlation should be ~1
    print('  %-12s crossCorr RG=%+.3f RB=%+.3f GB=%+.3f  | 竖条: 列间差 R%5.2f G%5.2f B%5.2f'
          % (tag, c(R, G), c(R, B), c(G, B),
             np.abs(np.diff(R.astype(float), axis=1)).mean(),
             np.abs(np.diff(G.astype(float), axis=1)).mean(),
             np.abs(np.diff(B.astype(float), axis=1)).mean()))
    # column-mean profile: for a colorbar this is a clean staircase
    for nm, P in (('R', R), ('G', G), ('B', B)):
        cm = P.mean(axis=0)
        print('      %s 列均值(每16列): %s' % (nm, ' '.join('%3d' % v for v in cm[::16].astype(int))))


print('=== 打开传感器彩条 ===', flush=True)
t = cmd('cbar 1', wait=4)
for l in t.splitlines():
    if '彩条' in l:
        print('  ' + l.strip(), flush=True)
time.sleep(3)

print()
print('=== 彩条模式下的原始帧 ===', flush=True)
for i, b in enumerate(grab('cbar')):
    rgb = rgb565(b)
    report(rgb, 'cbar_%d' % i)
    Image.fromarray(rgb).resize((640, 480), Image.NEAREST).save(OUT + '\\cbar_%d.png' % i)

print()
print('=== 关闭彩条 ===', flush=True)
cmd('cbar 0', wait=3)
time.sleep(2)
b = grab('normal', 1)[0]
rgb = rgb565(b)
report(rgb, 'normal')
Image.fromarray(rgb).resize((640, 480), Image.NEAREST).save(OUT + '\\normal.png')
