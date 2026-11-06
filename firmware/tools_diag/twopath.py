import serial, time, re, sys, urllib.request
import numpy as np
from PIL import Image
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
BASE = 'http://192.168.2.138:8080'


def serial_capture(n=2, timeout=40):
    s = serial.Serial('COM3', 115200, timeout=0.3)
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
                        print('  serial captured %d bytes' % len(body))
                        break
        time.sleep(0.3)
    s.close()
    return outs


def http_capture(n=2):
    outs = []
    for _ in range(n):
        d = urllib.request.urlopen(BASE + '/raw?capture', timeout=20).read()
        outs.append(d[:153600])
    return outs


def roughness(b):
    """mean abs diff between horizontally adjacent RGB565 pixels (per byte plane)"""
    f = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2).astype(np.float64)
    hi = f[:, :, 0]; lo = f[:, :, 1]
    return np.abs(np.diff(hi, axis=1)).mean(), np.abs(np.diff(lo, axis=1)).mean()


print('=== 同一时刻: 串口 rawdump vs HTTP /raw ===', flush=True)
sers = serial_capture(2)
https = http_capture(2)
print()
print('%-22s %10s %10s' % ('source', 'hi_adjdiff', 'lo_adjdiff'))
for i, b in enumerate(sers):
    h, l = roughness(b)
    print('  serial[%d]              %10.2f %10.2f' % (i, h, l))
for i, b in enumerate(https):
    h, l = roughness(b)
    print('  http[%d]                %10.2f %10.2f' % (i, h, l))

print()
print('固件 q 指标参考:  g2_energy ~0.55 (非常平滑) / r5_energy ~0.2')
print('=> 固件说帧是平滑的; 若 PC 也算出 hi_adjdiff 很大, 说明是【抓取路径】在坏')
