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
a = fr[0]
HI = a[:, :, 0].astype(np.int64)   # even bytes
LO = a[:, :, 1].astype(np.int64)   # odd bytes

def adj(P):
    return float(np.abs(P[:, 1:] - P[:, :-1]).mean())

print("\n=== If YUYV: odd bytes = U,V,U,V ... ===")
print("  LO[0::2] (U candidates) adj-diffs: %.2f" % adj(LO[:, 0::2]))
print("  LO[1::2] (V candidates) adj-diffs: %.2f" % adj(LO[:, 1::2]))
print("  HI (Y candidates)     adj-diffs: %.2f" % adj(HI))
print("  -> if U,V were smooth, these would be ~1-5")

print("\n=== Actual LO row 120, cols 100-120 (even/odd split) ===")
print("  U cand (LO[0::2]):", LO[120, 100:120:2].tolist())
print("  V cand (LO[1::2]):", LO[120, 101:120:2].tolist())

print("\n=== If RGB565: sub-fields ===")
print("  HI&7 (G3) unique:", np.unique(HI & 7).tolist())
print("  LO&0x1F (B5) adj: %.2f" % adj(LO & 0x1F))
print("  LO>>5 (G2+G3) adj: %.2f" % adj(LO >> 5))

print("\n=== KEY: does LO's low-5-bit (B5) look like a real image? ===")
B5 = LO & 0x1F
print("  B5 adj %.2f  corrX %.3f" % (adj(B5), np.corrcoef(B5[:, :-1].ravel(), B5[:, 1:].ravel())[0, 1]))
print("  B5 row120[100:116]:", B5[120, 100:116].tolist())

print("\n=== KEY: does HI's high-5-bit (R5) look like a real image? ===")
R5 = HI >> 3
print("  R5 adj %.2f  corrX %.3f" % (adj(R5), np.corrcoef(R5[:, :-1].ravel(), R5[:, 1:].ravel())[0, 1]))
print("  R5 row120[100:116]:", R5[120, 100:116].tolist())

print("\n=== Render YUYV decode of FRESH data ===")
Y = HI[:, 0::2].astype(np.float64)
U = LO[:, 0::2].astype(np.float64)
V = LO[:, 1::2].astype(np.float64)
ue = np.repeat(U, 2, axis=1); ve = np.repeat(V, 2, axis=1)
u = (ue - 128)[:, :, None]; v = (ve - 128)[:, :, None]; y = Y[:, :, None]
yuyv = np.clip(np.dstack([y + 1.402*v, y - 0.344136*u - 0.714136*v, y + 1.772*u]), 0, 255).astype(np.uint8)
Image.fromarray(yuyv).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\fresh_yuyv.png')

rgb = np.dstack([HI & 0xF8, ((HI & 7) << 5) | ((LO & 0xE0) >> 3), (LO & 0x1F) << 3]).astype(np.uint8)
Image.fromarray(rgb).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\fresh_rgb565.png')
Image.fromarray(HI.astype(np.uint8)).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\fresh_hi.png')
print("saved fresh_yuyv.png / fresh_rgb565.png / fresh_hi.png")
