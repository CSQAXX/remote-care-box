import numpy as np, urllib.request, sys, time, re, serial
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'


def fw_stats(b):
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
                if abs(g2 - (l+r)//2) > 3:
                    g2bad += 1
            tot += 1
        pairs += 319
    return (r5*100)/pairs/100.0, (g2bad*10000)//tot/100.0


def qcmd():
    s = serial.Serial('COM3', 115200, timeout=0.3)
    s.reset_input_buffer()
    s.write(b'q\r\n')
    s.flush()
    buf = b''
    t0 = time.time()
    while time.time() - t0 < 2:
        d = s.read(4096)
        if d:
            buf += d
    s.close()
    t = re.sub(r'\x1b\[[0-9;]*m', '', buf.decode('utf-8', 'replace'))
    m = re.search(r'g2_bad=([\d.]+)% g2_energy=([\d.]+) r5_energy=([\d.]+) b5_energy=([\d.]+)', t)
    if not m:
        return None
    return float(m.group(1)), float(m.group(3))


print('=== 并行取样: 固件自测 vs HTTP 抓取 (同一时段) ===', flush=True)
for i in range(8):
    fw = qcmd()
    d = urllib.request.urlopen(BASE + '/raw?capture', timeout=20).read()
    pc_r5, pc_g2 = fw_stats(d[:153600])
    print('  [%d] 固件: g2_bad=%5.2f%% r5_e=%.2f   ||  PC复算: g2_bad=%5.2f%% r5_e=%.2f'
          % (i, fw[0], fw[1], pc_g2, pc_r5), flush=True)

print()
print('解读:')
print('  若 PC 复算的 g2_bad 总是 >= 固件自测, 说明数据在【离开 PSRAM 帧缓冲】的路上变脏')
print('  若两者接近, 说明帧缓冲里的数据本身就是脏的 (问题在采集链路/传感器)')
