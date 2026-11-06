import serial, time, sys, re, os
import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'
PORT = 'COM3'


def capture(n=3, timeout=25):
    s = serial.Serial(PORT, 115200, timeout=0.3)
    s.reset_input_buffer()
    outs = []
    for k in range(n):
        s.write(b'rawdump\r\n')
        s.flush()
        buf = b''
        deadline = time.time() + timeout
        hdr = None
        while time.time() < deadline:
            d = s.read(65536)
            if d:
                buf += d
                if hdr is None:
                    m = re.search(rb'RAWDUMP (\d+) (\d+)', buf)
                    if m:
                        hdr = (int(m.group(1)), int(m.group(2)))
            if hdr is not None:
                nl = buf.find(b'\r\n', buf.find(b'RAWDUMP'))
                if nl != -1 and len(buf) >= nl + 4 + hdr[1]:
                    body = buf[nl + 4:nl + 4 + hdr[1]]
                    if len(body) == hdr[1]:
                        outs.append(body)
                        print('  captured %d bytes (seq %d)' % (len(body), hdr[0]))
                        break
        time.sleep(0.2)
    s.close()
    return outs


if __name__ == '__main__':
    outs = capture(3)
    for i, b in enumerate(outs):
        open(os.path.join(OUT, 'truth_%d.bin' % i), 'wb').write(b)
    print('total %d frames' % len(outs))
