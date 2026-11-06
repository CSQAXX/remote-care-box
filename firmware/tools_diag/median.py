import urllib.request, numpy as np, time, sys
from PIL import Image
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'


def grab():
    d = urllib.request.urlopen(BASE + '/raw?capture', timeout=20).read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


N = 20
print('抓 %d 帧做逐像素中位数...' % N, flush=True)
fs = np.stack([grab() for _ in range(N)])
print('  完成')

med = np.median(fs, axis=0).astype(np.uint8)
Image.fromarray(med[:, :, 0]).resize((640, 480), Image.NEAREST).save(OUT + '\\median_hi.png')
Image.fromarray(med[:, :, 1]).resize((640, 480), Image.NEAREST).save(OUT + '\\median_lo.png')

# how much did the median clean up vs a single frame?
f0 = fs[0]
print()
print('=== 中位数 vs 单帧: 噪声抑制效果 ===')
for name, idx in (('hi byte', 0), ('lo byte', 1)):
    single = f0[:, :, idx].astype(np.float32)
    m = med[:, :, idx].astype(np.float32)
    # local roughness = mean abs diff to neighbours
    def rough(P):
        return (np.abs(np.diff(P, axis=1)).mean() + np.abs(np.diff(P, axis=0)).mean()) / 2
    print('  %s: 单帧粗糙度 %6.2f -> 中位数粗糙度 %6.2f  (降低 %.0f%%)'
          % (name, rough(single), rough(m), (1 - rough(m) / max(rough(single), 1e-6)) * 100))

print()
print('=== 中位数图的位统计 (若中位数是干净图像, 位应该不再是随机 50% 翻转) ===')
for bi in range(16):
    byte_idx = bi // 8; bitpos = bi % 8
    vals = ((fs[:, :, :, byte_idx].astype(np.int32) >> bitpos) & 1)
    flip = (vals != np.roll(vals, 1, axis=0)).mean() * 100
    # median value of that bit across frames
    mv = np.median(vals, axis=0).mean() * 100
    print('  bit%2d: 跨帧翻转率 %5.2f%%   中位数取值 %5.1f%%' % (bi, flip, mv))

print()
print('=== 中位数图 vs 单帧 的相关性 ===')
for name, idx in (('hi', 0), ('lo', 1)):
    a = f0[:, :, idx].astype(float).ravel(); b = med[:, :, idx].astype(float).ravel()
    a -= a.mean(); b -= b.mean()
    print('  %s: corr(单帧, 中位数) = %+.4f' % (name, (a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum())))
