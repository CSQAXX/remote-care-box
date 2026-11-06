import serial, time, re, sys, urllib.request
import numpy as np
from PIL import Image
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'


def cmd(c, wait=4.0):
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


def grab(n=3):
    outs = []
    for i in range(n):
        d = urllib.request.urlopen(BASE + '/raw?capture', timeout=20).read()
        b = np.frombuffer(d[:153600], dtype=np.uint8)
        outs.append(b)
        open(OUT + '\\cb2_%d.bin' % i, 'wb').write(b.tobytes())
    return outs


def fw_stats(b):
    f = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2).astype(np.int64)
    step = 240 // 30
    r5 = 0; g2bad = 0; tot = 0
    for y in range(0, 240, step):
        row = f[y]
        prev = int((row[0][0] >> 3) & 0x1F)
        for x in range(1, 320):
            cur = int((row[x][0] >> 3) & 0x1F)
            r5 += abs(cur - prev); prev = cur
            g2 = int((row[x][1] >> 5) & 7)
            if x >= 2 and x + 1 < 320:
                l = int((row[x-1][1] >> 5) & 7); r = int((row[x+1][1] >> 5) & 7)
                if abs(g2 - (l+r)//2) > 3:
                    g2bad += 1
            tot += 1
    return (r5*100)/(319*step)/100.0, (g2bad*10000)//tot/100.0


def rgb(b):
    f = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2).astype(np.int64)
    A = f[:, :, 0]; B = f[:, :, 1]
    return np.dstack([((A >> 3) << 3).astype(np.uint8),
                      (((A & 7) << 3) | (B >> 5)).astype(np.uint8),
                      ((B & 31) << 3).astype(np.uint8)])


print('=== 打开传感器彩条 (固定图案, 不经过镜头) ===', flush=True)
t = cmd('cbar 1', wait=5)
for l in t.splitlines():
    if '彩条' in l or 'cbar' in l:
        print('  ' + l.strip(), flush=True)
time.sleep(3)

print()
print('=== 彩条模式的帧 ===', flush=True)
for i, b in enumerate(grab(3)):
    r5, g2 = fw_stats(b)
    im = rgb(b)
    Image.fromarray(im).resize((640, 480), Image.NEAREST).save(OUT + '\\cb2_%d.png' % i)
    # 彩条是竖条纹 -> 每一行应该几乎一样
    rows = im.astype(float)
    rowvar = rows.std(axis=1).mean()
    print('  cb2_%d  fw:r5_energy=%.2f g2_bad=%.2f%%  行间标准差=%.2f' % (i, r5, g2, rowvar))

print()
print('=== 关闭彩条, 正常画面 ===', flush=True)
cmd('cbar 0', wait=3)
time.sleep(2)
for i, b in enumerate(grab(2)):
    r5, g2 = fw_stats(b)
    im = rgb(b)
    Image.fromarray(im).resize((640, 480), Image.NEAREST).save(OUT + '\\nb2_%d.png' % i)
    rows = im.astype(float)
    rowvar = rows.std(axis=1).mean()
    print('  nb2_%d  fw:r5_energy=%.2f g2_bad=%.2f%%  行间标准差=%.2f' % (i, r5, g2, rowvar))
