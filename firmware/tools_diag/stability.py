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
    return tuple(float(m.group(i)) for i in (1, 2, 3, 4)) if m else None


time.sleep(2)
print('=== 稳定性观察: 连续 40 次采样 ===', flush=True)
vals = []
for i in range(40):
    q = quality()
    if q:
        vals.append(q)
    if (i + 1) % 10 == 0:
        print('  已采样 %d 次' % (i + 1), flush=True)

if vals:
    bad = [v[0] for v in vals]
    r5 = [v[2] for v in vals]
    print()
    print('  g2_bad:  最小 %.2f%%  最大 %.2f%%  平均 %.2f%%' % (min(bad), max(bad), sum(bad) / len(bad)))
    print('  r5_e:    最小 %.2f  最大 %.2f  平均 %.2f' % (min(r5), max(r5), sum(r5) / len(r5)))
    clean = sum(1 for b in bad if b < 5)
    print('  g2_bad < 5%% 的帧: %d/%d (%.0f%%)' % (clean, len(bad), clean * 100.0 / len(bad)))

print()
print('=== 设备健康状态 ===')
t = cmd('stat', 3)
for l in t.splitlines():
    if 'cam_frames' in l:
        print('  ' + l.strip())

s.close()
