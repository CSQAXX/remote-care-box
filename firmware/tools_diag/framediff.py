import urllib.request, numpy as np, time, sys
from PIL import Image
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'


def grab():
    d = urllib.request.urlopen(BASE + '/raw?capture', timeout=20).read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


print('=== 连续抓 5 帧, 逐像素比对 ===')
fs = [grab() for _ in range(5)]
for i in range(1, 5):
    a = fs[0].astype(np.int16); b = fs[i].astype(np.int16)
    d = np.abs(a - b)
    print('  frame0 vs frame%d: 字节不同率 %5.2f%%  平均差 %5.2f  最大差 %3d'
          % (i, (d != 0).mean() * 100, d.mean(), d.max()))

print()
print('=== 噪声是固定图案还是逐帧随机? ===')
a = fs[0].astype(np.int16); b = fs[1].astype(np.int16)
d = np.abs(a - b)
# per-pixel std across 5 frames
stack = np.stack([f.astype(np.float32) for f in fs])
per_pixel_std = stack.std(axis=0).mean(axis=2)   # per-pixel std across frames, per byte
print('  同一像素位置跨 5 帧的标准差: 均值 %.2f' % per_pixel_std.mean())

# If noise is a FIXED PATTERN, the diff image is the same each time
d01 = np.abs(fs[0].astype(np.int16) - fs[1].astype(np.int16))
d12 = np.abs(fs[1].astype(np.int16) - fs[2].astype(np.int16))
print('  |f0-f1| 与 |f1-f2| 的相关性: %.3f' % np.corrcoef(d01.ravel(), d12.ravel())[0, 1])
print('  (接近 1 = 固定图案/结构性错位; 接近 0 = 逐帧随机噪声)')

print()
print('=== 哪些位在变? (逐位统计跨帧翻转率) ===')
for bi in range(16):
    bit = 0
    for fi, f in enumerate(fs):
        pass
    # bit b of the 16-bit stream = bit (b%8) of byte (b//8)
    byte_idx = bi // 8
    bitpos = bi % 8
    vals = ((stack[:, :, :, byte_idx].astype(np.int32) >> bitpos) & 1)
    flip = (vals != np.roll(vals, 1, axis=0)).mean() * 100
    print('  bit%2d (byte%d bit%d): 跨帧翻转率 %5.2f%%' % (bi, byte_idx, bitpos, flip))

print()
print('=== 差分图 (f0 - f1) 可视化: 结构性错位会长条, 随机噪声是雪花 ===')
diff = np.abs(fs[0].astype(np.int16) - fs[1].astype(np.int16)).max(axis=2).astype(np.uint8)
Image.fromarray(diff).resize((640, 480), Image.NEAREST).save(OUT + '\\diff01.png')
# row profile of the diff
rp = diff.mean(axis=1)
print('  逐行平均差: ', ' '.join('%3d' % v for v in rp[::12].astype(int)))
cp = diff.mean(axis=0)
print('  逐列平均差: ', ' '.join('%3d' % v for v in cp[::16].astype(int)))
