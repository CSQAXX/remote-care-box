import serial, time, sys, re, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
PORT = 'COM3'


def send(cmds, wait=1.0):
    s = serial.Serial(PORT, 115200, timeout=0.3)
    s.reset_input_buffer()
    for c in cmds:
        s.write((c + '\r\n').encode())
        s.flush()
        time.sleep(wait)
    return s


def capture(name, pre_cmds=(), post_cmds=()):
    """Capture a raw dump over serial, optionally after setting state."""
    s = send(pre_cmds, wait=1.2)
    s.reset_input_buffer()
    s.write(b'rawdump\r\n')
    s.flush()

    buf = b''
    deadline = time.time() + 25
    hdr = None
    while time.time() < deadline:
        d = s.read(65536)
        if d:
            buf += d
            if hdr is None:
                m = re.search(rb'RAWDUMP (\d+) (\d+)', buf)
                if m:
                    hdr = (int(m.group(1)), int(m.group(2)))
                    print('  header: seq=%d len=%d' % hdr)
        if hdr is not None:
            idx = buf.find(b'RAWDUMP')
            nl = buf.find(b'\r\n', idx)
            if nl != -1 and len(buf) >= nl + 4 + hdr[1]:
                body = buf[nl + 4: nl + 4 + hdr[1]]
                if len(body) == hdr[1]:
                    path = os.path.join(OUT, name)
                    open(path, 'wb').write(body)
                    print('  saved %s (%d bytes)' % (name, len(body)))
                    s.close()
                    return path
    s.close()
    print('  TIMEOUT for %s' % name)
    return None


if __name__ == '__main__':
    tag = sys.argv[1] if len(sys.argv) > 1 else 'cap'
    pre = sys.argv[2:] if len(sys.argv) > 2 else []
    capture('%s_conv%d.bin' % (tag, 1 if not pre or 'conv 1' not in pre else 0), pre)
