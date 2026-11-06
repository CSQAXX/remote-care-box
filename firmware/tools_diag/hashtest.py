import serial, time, re, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

s = serial.Serial('COM3', 115200, timeout=0.3)
s.reset_input_buffer()

print('=== 固件 hash 测试: 同一帧缓冲隔 250ms 算两次哈希 ===', flush=True)
for trial in range(3):
    s.reset_input_buffer()
    s.write(b'hash\r\n')
    s.flush()
    buf = b''
    t0 = time.time()
    while time.time() - t0 < 5:
        d = s.read(8192)
        if d:
            buf += d
    t = re.sub(r'\x1b\[[0-9;]*m', '', buf.decode('utf-8', 'replace'))
    for l in t.splitlines():
        if 'HASH' in l:
            print('  trial%d: %s' % (trial, l.strip()), flush=True)
    time.sleep(1)

s.close()
