import serial, time, sys, re, os
import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'
PORT = 'COM3'


def capture_gray(path, n=3, timeout=25):
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
                        break
        time.sleep(0.2)
    s.close()
    for i, b in enumerate(outs):
        open(path % i, 'wb').write(b)
    return outs


def analyze_gray(bodies):
    print('=== GRAYSCALE ANALYSIS (1 byte/pixel, no packing, no converter) ===')
    print('%-6s %10s %10s %10s %10s %12s' % ('frm', 'adjDiff', 'vertCorr', 'wildRows', 'min', 'max'))
    for i, b in enumerate(bodies):
        P = np.frombuffer(b[:76800], dtype=np.uint8).reshape(240, 320).astype(np.int64)
        adj = np.abs(np.diff(P, axis=1)).mean()
        vert = np.corrcoef(P[:-1].ravel().astype(float), P[1:].ravel().astype(float))[0, 1]
        rowadj = np.abs(np.diff(P, axis=1)).mean(axis=1)
        wild = (rowadj > 25).sum()
        print('%-6d %10.2f %10.3f %10d %10d %12d' % (i, adj, vert, wild, P.min(), P.max()))
        Image.fromarray(P.astype(np.uint8)).resize((640, 480), Image.NEAREST).save(
            OUT + '\\gray_%d.png' % i)
    # cross-frame
    if len(bodies) >= 2:
        A = np.frombuffer(bodies[0][:76800], dtype=np.uint8).reshape(240, 320)
        B = np.frombuffer(bodies[1][:76800], dtype=np.uint8).reshape(240, 320)
        print()
        print('frame0 vs frame1: byte-identical %.1f%%   corr %.3f' % (
            (A == B).mean() * 100, np.corrcoef(A.ravel().astype(float), B.ravel().astype(float))[0, 1]))
        print('NOTE: the scene changes (person moving), so low corr is EXPECTED.')
        print('The key number is adjDiff / wildRows: a clean link gives low, uniform values.')


if __name__ == '__main__':
    bodies = capture_gray(OUT + '\\gray_%d.bin', n=3)
    print('captured %d frames' % len(bodies))
    analyze_gray(bodies)
