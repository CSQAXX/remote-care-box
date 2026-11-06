import urllib.request, numpy as np, os, time
from PIL import Image

BASE = 'http://192.168.2.138:8080'
OUT = r'D:\Software\kanhu\remote-care-box'


def raw():
    d = urllib.request.urlopen(BASE + '/raw?capture', timeout=15).read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


def hf(P):
    """high-frequency energy (laplacian abs mean)"""
    p = P.astype(float)
    return float(np.abs(p[1:-1,1:-1]*4 - p[:-2,1:-1] - p[2:,1:-1]
                       - p[1:-1,:-2] - p[1:-1,2:]).mean())


def quant(f, tag, verbose=True):
    """
    NORMALIZED comparison: green is 6-bit (64 levels), red/blue are 5-bit (32).
    So green must be scaled by 32/64 = 0.5 to be comparable.
    """
    A = f[:, :, 0].astype(np.int64); B = f[:, :, 1].astype(np.int64)
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    G6 = (G3 << 3) | G2

    # scale everything to 5-bit equivalent
    R5s = R5.astype(float)
    G5s = G6.astype(float) / 2.0     # 6-bit -> 5-bit equivalent
    B5s = B5.astype(float)

    hR = hf(R5s); hG = hf(G5s); hB = hf(B5s)
    dR = np.abs(np.diff(R5s, axis=1)).mean()
    dG = np.abs(np.diff(G5s, axis=1)).mean()
    dB = np.abs(np.diff(B5s, axis=1)).mean()

    rgb = np.dstack([((R5) << 3).astype(np.uint8),
                     (G6).astype(np.uint8),
                     ((B5) << 3).astype(np.uint8)])
    if verbose:
        print('  %-10s HF: R %6.2f  G %6.2f  B %6.2f  -> G/R %.2f  G/B %.2f   |  adj: R %5.2f G %5.2f B %5.2f -> G/R %.2f' % (
            tag, hR, hG, hB, hG / max(hR, .01), hG / max(hB, .01),
            dR, dG, dB, dG / max(dR, .01)))
    return rgb, hG / max(hR, .01), hG / max(hB, .01)


print('=== NORMALIZED green-vs-red/blue comparison ===')
print('  (green scaled 6bit->5bit; ratio ~1.0 = clean, >>1 = flower-screen)')
print()

fs = []
for i in range(3):
    f = raw(); fs.append(f)
    open(os.path.join(OUT, 'now_%d.bin' % i), 'wb').write(f.tobytes())
    time.sleep(0.2)
for i, f in enumerate(fs):
    rgb, rR, rB = quant(f, 'frame%d' % i)
    Image.fromarray(rgb).resize((640, 480), Image.NEAREST).save(OUT + '\\now_%d.png' % i)

print()
print('  --- old dump for reference ---')
old = np.frombuffer(open(OUT + '\\raw3_1.bin','rb').read()[:153600],
                     dtype=np.uint8).reshape(240,320,2)
quant(old, 'raw3_1(old)')
