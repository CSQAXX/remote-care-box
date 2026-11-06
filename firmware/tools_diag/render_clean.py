import numpy as np, glob
from PIL import Image

def load(pat):
    out = []
    for f in sorted(glob.glob(pat)):
        d = open(f, 'rb').read()
        i = d.find(b'\r\n\r\n')
        body = d[i + 4:]
        if len(body) < 153600:
            continue
        out.append(np.frombuffer(body[:153600], dtype=np.uint8).reshape(240, 320, 2))
    return np.array(out)

fr = load(r'D:\Software\kanhu\remote-care-box\raw3_*.bin')
print("frames:", len(fr))
b0 = fr[0]
HI = b0[:, :, 0].astype(np.int64)
LO = b0[:, :, 1].astype(np.int64)

R = ((HI >> 3) & 0x1F) << 3
G = (((HI & 7) << 3) | ((LO >> 5) & 0x07))
B = (LO & 0x1F) << 3
rgb = np.dstack([R, G, B]).astype(np.uint8)
print("decoded shape", rgb.shape, "mean", rgb.mean(axis=(0,1)))
Image.fromarray(rgb).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\decoded.png')

# also big-endian variant
R2 = ((HI & 0xF8))
G2b = (((HI & 7) << 5) | ((LO & 0xE0) >> 3))
B2 = (LO & 0x1F) << 3
Image.fromarray(np.dstack([R2, G2b, B2]).astype(np.uint8)).resize((960,720), Image.NEAREST).save(
    r'D:\Software\kanhu\remote-care-box\decoded_be.png')
print("saved decoded.png / decoded_be.png")

# per-frame check: do consecutive frames differ a lot (sensor noise) or a little?
if len(fr) >= 2:
    d = np.abs(fr[0].astype(np.int64) - fr[1].astype(np.int64)).mean()
    print("mean |frame0-frame1| = %.2f" % d)

# check for horizontal tear lines: rows that are shifted versions of neighbours
print("\n=== TEAR CHECK: is any row a horizontal shift of its neighbour? ===")
gray = rgb.mean(axis=2)
for r in range(1, 240):
    best = 1e9; bs = 0
    for s in range(-6, 7):
        if s >= 0:
            a = gray[r, :320-s]; b = gray[r-1, s:]
        else:
            a = gray[r, -s:]; b = gray[r-1, :320+s]
        v = np.abs(a-b).mean()
        if v < best: best, bs = v, s
    if best > 25 and bs != 0:
        print("  row %d: best shift %+d err %.1f" % (r, bs, best))
print("  (no output above = no tear lines)")
