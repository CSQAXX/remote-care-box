import urllib.request, numpy as np, sys, os, time
from PIL import Image

BASE = 'http://192.168.2.138:8080'
OUT = r'D:\Software\kanhu\remote-care-box'


def raw():
    d = urllib.request.urlopen(BASE + '/raw?capture', timeout=15).read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


def quant(f, tag):
    """Quantify the flower-screen in the RGB565 interpretation."""
    A = f[:, :, 0].astype(np.int64); B = f[:, :, 1].astype(np.int64)
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    G = (G3 << 3) | G2
    rgb = np.dstack([((R5) << 3).astype(np.uint8),
                     G.astype(np.uint8),
                     ((B5) << 3).astype(np.uint8)])
    # FLOWER METRIC: how much does the green channel jitter pixel-to-pixel
    # relative to red/blue? In a clean image green is as smooth as red/blue.
    dg = np.abs(np.diff(G, axis=1)).mean()
    dr = np.abs(np.diff(R5, axis=1)).mean()
    db = np.abs(np.diff(B5, axis=1)).mean()
    # high-frequency energy of green (the visual signature of flower-screen)
    g = G.astype(float)
    lapg = np.abs(g[1:-1,1:-1]*4 - g[:-2,1:-1] - g[2:,1:-1] - g[1:-1,:-2] - g[1:-1,2:]).mean()
    r = R5.astype(float)
    lapr = np.abs(r[1:-1,1:-1]*4 - r[:-2,1:-1] - r[2:,1:-1] - r[1:-1,:-2] - r[1:-1,2:]).mean()
    print('  %-10s  greenAdj %6.2f  redAdj %6.2f  blueAdj %6.2f  |  greenHF %6.2f redHF %6.2f  |  G/Red HF ratio %5.2f' % (
        tag, dg, dr, db, lapg, lapr, lapg / max(lapr, 0.01)))
    return rgb


print('=== CURRENT FIRMWARE: is the picture clean now? ===')
fs = []
for i in range(3):
    f = raw()
    fs.append(f)
    open(os.path.join(OUT, 'now_%d.bin' % i), 'wb').write(f.tobytes())
    time.sleep(0.2)

for i, f in enumerate(fs):
    rgb = quant(f, 'frame%d' % i)
    Image.fromarray(rgb).resize((640, 480), Image.NEAREST).save(OUT + '\\now_%d.png' % i)

print()
print('=== OLD DUMP (for reference) ===')
old = np.frombuffer(open(OUT + '\\raw3_1.bin', 'rb').read()[:153600],
                     dtype=np.uint8).reshape(240, 320, 2)
quant(old, 'raw3_1')

print()
print('=== GREEN HF RATIO: the flower-screen discriminator ===')
print('  clean image : greenHF ~= redHF        (ratio ~1)')
print('  flower-screen: greenHF >> redHF       (ratio >> 1)')
