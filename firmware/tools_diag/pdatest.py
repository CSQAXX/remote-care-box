import serial, time, re, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PORT = 'COM3'


class Dev:
    def __init__(self):
        self.s = serial.Serial(PORT, 115200, timeout=0.2)
        self.buf = b''

    def cmd(self, c, wait=3.0):
        self.s.reset_input_buffer()
        self.s.write((c + '\r\n').encode())
        self.s.flush()
        t0 = time.time()
        while time.time() - t0 < wait:
            d = self.s.read(8192)
            if d:
                self.buf += d
        txt = re.sub(r'\x1b\[[0-9;]*m', '', self.buf.decode('utf-8', 'replace'))
        self.buf = b''
        return txt

    def close(self):
        self.s.close()


def quality(d):
    t = d.cmd('q', wait=2.0)
    m = re.search(r'g2_bad=([\d.]+)% g2_energy=([\d.]+) r5_energy=([\d.]+) b5_energy=([\d.]+)', t)
    return (float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))) if m else None


d = Dev()
print('=== 基线 (PSRAM DMA = 开) ===', flush=True)
for i in range(4):
    q = quality(d)
    print('  g2_bad=%s%% g2_e=%s r5_e=%s b5_e=%s' % q if q else '  parse fail', flush=True)

print()
print('=== 切换 pda 0 (关闭 PSRAM DMA 直写) ===', flush=True)
t = d.cmd('pda 0', wait=15)
for l in t.splitlines():
    if any(k in l for k in ('PSRAM', '切换', '失败', 'panic', 'Guru', 'Restart', 'boot:')):
        print('  ' + l.strip(), flush=True)

time.sleep(4)
for i in range(4):
    q = quality(d)
    print('  g2_bad=%s%% g2_e=%s r5_e=%s b5_e=%s' % q if q else '  parse fail', flush=True)

print()
print('=== 切回 pda 1 对照 ===', flush=True)
d.cmd('pda 1', wait=15)
time.sleep(4)
for i in range(3):
    q = quality(d)
    print('  g2_bad=%s%% g2_e=%s r5_e=%s b5_e=%s' % q if q else '  parse fail', flush=True)

d.close()
