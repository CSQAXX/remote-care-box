import numpy as np
import serial, time, sys, re, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT = r'D:\Software\kanhu\remote-care-box'
PORT = 'COM3'


def capture_to(path, s, timeout=25):
    s.reset_input_buffer()
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
            idx = buf.find(b'RAWDUMP')
            nl = buf.find(b'\r\n', idx)
            if nl != -1 and len(buf) >= nl + 4 + hdr[1]:
                body = buf[nl + 4:nl + 4 + hdr[1]]
                if len(body) == hdr[1]:
                    open(path, 'wb').write(body)
                    return True
    return False


def load(path):
    d = open(path, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


def fields(f):
    A = f[:, :, 0].astype(np.int64)
    B = f[:, :, 1].astype(np.int64)
    return {'R5': A >> 3, 'G3': A & 7, 'G2': B >> 5, 'B5': B & 31}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'conv1'
    cmds = ['conv 1'] if mode == 'conv1' else ['conv 0']
    s = serial.Serial(PORT, 115200, timeout=0.3)
    for c in cmds:
        s.write((c + '\r\n').encode()); s.flush(); time.sleep(2.0)

    paths = []
    for i in range(3):
        p = os.path.join(OUT, 'seq_%s_%d.bin' % (mode, i))
        ok = capture_to(p, s)
        print('  capture %d: %s' % (i, 'OK' if ok else 'FAIL'))
        if ok:
            paths.append(p)
        time.sleep(0.3)
    s.close()

    if len(paths) < 2:
        print('not enough captures')
        return

    fs = [fields(load(p)) for p in paths]
    print()
    print('=== TEMPORAL CORRELATION BETWEEN CONSECUTIVE FRAMES ===')
    print('Garbage re-randomizes every frame => corr ~0.')
    print('A real channel barely changes => corr high.')
    print()
    names = ['R5', 'G3', 'G2', 'B5']
    print('%-6s %10s %10s %10s' % ('field', 'f0-f1', 'f1-f2', 'mean'))
    for nm in names:
        cs = []
        for i in range(len(fs) - 1):
            a = fs[i][nm].ravel().astype(float)
            b = fs[i + 1][nm].ravel().astype(float)
            cs.append(np.corrcoef(a, b)[0, 1])
        print('%-6s %10.3f %10.3f %10.3f' % (nm, cs[0], cs[1], np.mean(cs)))

    print()
    print('=== G2 histogram per frame ===')
    for i, p in enumerate(paths):
        g2 = fs[i]['G2'].ravel()
        print('  frame %d: %s' % (i, np.bincount(g2, minlength=8).tolist()))

    print()
    print('=== G2 BAD PERCENT (firmware metric equivalent) ===')
    for i, p in enumerate(paths):
        P = fs[i]['G2']
        m = np.median(np.stack([P[:, :-2], P[:, 2:]]), axis=0)
        r = np.abs(P[:, 1:-1] - m)
        print('  frame %d: %.2f%%' % (i, (r > 3).mean() * 100))


main()
