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
        txt = self.buf.decode('utf-8', 'replace')
        txt = re.sub(r'\x1b\[[0-9;]*m', '', txt)
        self.buf = b''
        return txt

    def close(self):
        self.s.close()


def quality(d):
    """send 'q' and parse the QUAL line"""
    t = d.cmd('q', wait=2.0)
    m = re.search(r'QUAL: g2_bad=([\d.]+)% g2_energy=([\d.]+) r5_energy=([\d.]+) b5_energy=([\d.]+)', t)
    if not m:
        return None, t
    return (float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))), t


d = Dev()
print('=== 基准状态 (mode 0 = YUV422+硬件转换器) ===', flush=True)

# make sure we're in mode 0
t = d.cmd('mode 0', wait=8)
print('  mode:', 'OK' if '切换完成' in t else t.strip()[-120:], flush=True)
time.sleep(2)

for i in range(3):
    q, raw = quality(d)
    print('  WiFi ON,  LCD ON   -> g2_bad=%s g2_e=%s r5_e=%s b5_e=%s' % q if q else '  parse fail: %s' % raw[-200:], flush=True)

print()
print('=== 实验 A: 关掉 Wi-Fi (排除 PSRAM 总线争用) ===', flush=True)
d.cmd('wifioff', wait=3)
time.sleep(3)
for i in range(3):
    q, raw = quality(d)
    print('  WiFi OFF, LCD ON   -> g2_bad=%s g2_e=%s r5_e=%s b5_e=%s' % q if q else '  parse fail', flush=True)

print()
print('=== 实验 B: 再关掉 LCD (排除 SPI 抢占) ===', flush=True)
d.cmd('lcd1', wait=3)   # 固定图样, 不碰摄像头
time.sleep(2)
for i in range(3):
    q, raw = quality(d)
    print('  WiFi OFF, LCD PAT  -> g2_bad=%s g2_e=%s r5_e=%s b5_e=%s' % q if q else '  parse fail', flush=True)

print()
print('=== 恢复 ===', flush=True)
d.cmd('lcd0', wait=2)
d.cmd('wifix', wait=5)
time.sleep(3)
q, raw = quality(d)
print('  WiFi ON,  LCD ON   -> g2_bad=%s g2_e=%s r5_e=%s b5_e=%s' % q if q else '  parse fail', flush=True)
d.close()
