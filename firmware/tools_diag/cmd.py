import serial, time, sys, re
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

def strip(t):
    return re.sub(r'\x1b\[[0-9;]*m', '', t)

def run(cmds, wait=1.2, after=2.5):
    s = serial.Serial('COM3', 115200, timeout=0.3)
    s.reset_input_buffer()
    out = []
    for c in cmds:
        s.write((c + '\r\n').encode())
        s.flush()
        time.sleep(wait)
        buf = b''
        t0 = time.time()
        while time.time() - t0 < 0.6:
            d = s.read(4096)
            if d:
                buf += d
        out.append((c, strip(buf.decode('utf-8', 'replace'))))
    # final drain
    time.sleep(after)
    buf = b''
    t0 = time.time()
    while time.time() - t0 < 1.5:
        d = s.read(4096)
        if d:
            buf += d
    s.close()
    out.append(('(tail)', strip(buf.decode('utf-8', 'replace'))))
    return out

if __name__ == '__main__':
    cmds = sys.argv[1:] or ['q']
    for c, t in run(cmds):
        print('===== %s =====' % c)
        # only print interesting lines
        for line in t.splitlines():
            if any(k in line for k in ('QUAL', 'GFIX', '转换器', '切换', '暂停', 'PSRAM', '崩溃', 'rst:', 'Guru')):
                print('  ' + line.strip())
