import numpy as np, urllib.request, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'


def fw_r5_energy(b):
    """EXACT replica of firmware measure_channel_quality r5_energy"""
    f = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2).astype(np.int64)
    step = 240 // 30
    r5_sum = 0
    pairs = 0
    for y in range(0, 240, step):
        row = f[y]
        prev = int((row[0][0] >> 3) & 0x1F)
        for x in range(1, 320):
            cur = int((row[x][0] >> 3) & 0x1F)
            r5_sum += abs(cur - prev)
            prev = cur
        pairs += 319
    return (r5_sum * 100) / pairs / 100.0


def fw_g2_bad(b):
    """EXACT replica of firmware g2_bad_pct"""
    f = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2).astype(np.int64)
    step = 240 // 30
    bad = 0
    total = 0
    for y in range(0, 240, step):
        row = f[y]
        for x in range(1, 320):
            g2 = int((row[x][1] >> 5) & 7)
            if x >= 2 and x + 1 < 320:
                left = int((row[x - 1][1] >> 5) & 7)
                right = int((row[x + 1][1] >> 5) & 7)
                expect = (left + right) // 2
                if abs(g2 - expect) > 3:
                    bad += 1
            total += 1
    return (bad * 10000) // total / 100.0


def raw_8bit_roughness(b):
    f = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2).astype(np.int64).astype(float)
    return (np.abs(np.diff(f[:, :, 0], axis=1)).mean(),
            np.abs(np.diff(f[:, :, 1], axis=1)).mean())


print('=== 用【和固件完全一样的算法】重算 PC 抓到的数据 ===', flush=True)
print('固件自己报:  r5_energy=0.20~0.24  g2_bad=4~5%')
print()

for i in range(4):
    d = urllib.request.urlopen(BASE + '/raw?capture', timeout=20).read()
    b = d[:153600]
    r5 = fw_r5_energy(b)
    g2 = fw_g2_bad(b)
    h, l = raw_8bit_roughness(b)
    print('  http[%d]  fw算法: r5_energy=%.2f g2_bad=%.2f%%   |  原始字节差: hi=%.2f lo=%.2f'
          % (i, r5, g2, h, l))
    open(OUT + '\\same_%d.bin' % i, 'wb').write(b)

print()
print('如果复算结果 == 固件报数 -> 数据本身如此, "原始字节差"是伪像')
print('如果复算结果 << 固件报数 -> 抓到的数据比固件看到的更干净 -> 抓取路径在坏')
