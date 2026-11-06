import numpy as np, re

OUT = r'D:\Software\kanhu\remote-care-box'

for i in range(3):
    fn = OUT + '\\truth_%d.bin' % i
    b = open(fn, 'rb').read()[:153600]
    # look for ASCII log-line signatures embedded in the binary
    hits = re.findall(rb'[A-Za-z ]{12,}', b)
    sig = [h for h in hits if any(k in h for k in (b'I (', b'W (', b'E (', b'cam_', b'kanhu', b'lcd', b'wifi', b'http'))]
    print('%d bytes=%d  ascii-runs=%d  log-like=%d' % (i, len(b), len(hits), len(sig)))
    for s in sig[:12]:
        print('    >> %r' % s[:90])
