import serial, time, re, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

s = serial.Serial('COM3', 115200, timeout=0.3)


def cmd(c, wait=3.0):
    s.reset_input_buffer()
    s.write((c + '\r\n').encode())
    s.flush()
    t0 = time.time()
    buf = b''
    while time.time() - t0 < wait:
        d = s.read(8192)
        if d:
            buf += d
    return re.sub(r'\x1b\[[0-9;]*m', '', buf.decode('utf-8', 'replace'))


def quality(wait=2.0):
    t = cmd('q', wait)
    m = re.search(r'g2_bad=([\d.]+)% g2_energy=([\d.]+) r5_energy=([\d.]+) b5_energy=([\d.]+)', t)
    if not m:
        return None
    return tuple(float(m.group(i)) for i in (1, 2, 3, 4))


# 等设备启动
time.sleep(2)
for _ in range(20):
    t = cmd('', 1.0)
    if 'QUAL' in t or 'kanhu' in t:
        break

print('=== PCLK 分频扫描 (寄存器 0x28) ===', flush=True)
print('固件开机时已自动设为二分频 (GC0308_PCLK_DIV=1)', flush=True)
time.sleep(2)

for div in [1, 0, 2, 3, 1]:
    label = {0: '不分频 (PCLK~48MHz)', 1: '二分频 (PCLK~24MHz)',
             2: '四分频 (PCLK~12MHz)', 3: '八分频 (PCLK~6MHz)'}.get(div, '?')
    t = cmd('pclk %d' % div, 3.0)
    rb = re.search(r'0x28: 写入 (\d+), 回读 0x([0-9a-f]+)', t)
    print()
    print('--- pclk %d : %s  %s' % (div, label,
          ('(回读 0x%s)' % rb.group(2)) if rb else '(回读失败)'), flush=True)
    time.sleep(2.5)
    vals = []
    for i in range(5):
        q = quality()
        if q:
            vals.append(q)
            print('    g2_bad=%5.2f%%  g2_e=%5.2f  r5_e=%5.2f  b5_e=%5.2f' % q, flush=True)
    if vals:
        avg = sum(v[0] for v in vals) / len(vals)
        worst = max(v[0] for v in vals)
        print('    >> 平均 g2_bad = %.2f%%   最差 = %.2f%%' % (avg, worst), flush=True)

s.close()
