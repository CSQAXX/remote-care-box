import numpy as np, urllib.request, sys, time, re, serial
from PIL import Image
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'


def cmd(c, wait=4.0):
    s = serial.Serial('COM3', 115200, timeout=0.2)
    s.reset_input_buffer()
    s.write((c + '\r\n').encode())
    s.flush()
    t0 = time.time(); buf = b''
    while time.time() - t0 < wait:
        d = s.read(8192)
        if d: buf += d
    s.close()
    return re.sub(r'\x1b\[[0-9;]*m', '', buf.decode('utf-8', 'replace'))


def grab(n):
    outs = []
    for _ in range(n):
        d = urllib.request.urlopen(BASE + '/raw?capture', timeout=20).read()
        outs.append(np.frombuffer(d[:153600], dtype=np.uint8))
    return outs


def fw_quality(b):
    f = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2).astype(np.int64)
    step = 240 // 30
    r5 = 0; g2bad = 0; tot = 0; pairs = 0
    for y in range(0, 240, step):
        row = f[y]
        prev = int((row[0][0] >> 3) & 0x1F)
        for x in range(1, 320):
            cur = int((row[x][0] >> 3) & 0x1F)
            r5 += abs(cur - prev); prev = cur
            g2 = int((row[x][1] >> 5) & 7)
            if x >= 2 and x + 1 < 320:
                l = int((row[x-1][1] >> 5) & 7); r = int((row[x+1][1] >> 5) & 7)
                if abs(g2 - (l+r)//2) > 3: g2bad += 1
            tot += 1
        pairs += 319
    return (r5*100)/pairs/100.0, (g2bad*10000)//tot/100.0


print('=== 开彩条, 连抓 12 帧, 挑最干净的一帧做解码 ===', flush=True)
cmd('cbar 1', wait=4)
time.sleep(2)
frames = grab(12)
scored = []
for i, b in enumerate(frames):
    r5, g2 = fw_quality(b)
    scored.append((r5 + g2, i, b, r5, g2))
scored.sort()
print('  质量排序 (越小越干净):')
for s, i, b, r5, g2 in scored[:5]:
    print('    frame%-2d r5_e=%5.2f g2_bad=%5.2f%%  sum=%6.2f' % (i, r5, g2, s))

best = scored[0]
b = best[2]
open(OUT + '\\cb_best.bin', 'wb').write(b.tobytes())

print()
print('=== 彩条原始字节: 逐列前 64 字节 (前 16 个像素) ===')
f = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2)
row = f[120]
for x in range(16):
    print('  px%-3d hi=%3d(%s) lo=%3d(%s)' % (x, row[x,0], format(int(row[x,0]), '08b'), row[x,1], format(int(row[x,1]), '08b')))

print()
print('=== 彩条竖条结构: 各列的原始字节均值 (每 20 列) ===')
for byi in (0, 1):
    cm = f[:, :, byi].mean(axis=0)
    print('  byte%d 每20列均值: %s' % (byi, ' '.join('%3d' % v for v in cm[::20].astype(int))))

print()
print('=== 假设: 彩条是 8 个竖条, 看列方向是否有 8 段结构 ===')
for byi in (0, 1):
    cm = f[:, :, byi].mean(axis=0)
    # find segments where value is roughly constant
    segs = []
    cur = cm[0]
    start = 0
    for x in range(1, 320):
        if abs(cm[x] - cur) > 6:
            segs.append((start, x-1, cur))
            cur = cm[x]; start = x
    segs.append((start, 319, cur))
    print('  byte%d: %d 段: %s' % (byi, len(segs),
          ' '.join('%d-%d:%.0f' % (a, bb, v) for a, bb, v in segs[:14])))

cmd('cbar 0', wait=3)
