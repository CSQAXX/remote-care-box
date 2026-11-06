import numpy as np, glob
from PIL import Image

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
d = open(fs[0], 'rb').read()
i = d.find(b'\r\n\r\n')
b = d[i + 4:i + 4 + 153600]
a = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2)
H = a[:, :, 0].astype(np.float64)   # even bytes -> Y0,Y1,Y2,... (320)
L = a[:, :, 1].astype(np.float64)   # odd bytes  -> U,V,U,V,...  (320)

def lapstd(g):
    g = np.asarray(g, dtype=np.float64)
    return float((g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1]
                  - g[1:-1, :-2] - g[1:-1, 2:]).std())

# ---- YUYV (correct indexing) ----
# byte stream: Y0 U Y1 V Y2 U Y3 V ...
# H = Y0..Y319 (320 luma)
# L = U,V,U,V,...  -> L[:,0::2]=U (160, each shared by a pixel pair), L[:,1::2]=V
U = L[:, 0::2]          # 160 wide
V = L[:, 1::2]          # 160 wide
ue = np.repeat(U, 2, axis=1)     # 320, each U shared by 2 pixels
ve = np.repeat(V, 2, axis=1)
u = (ue - 128)[:, :, None]
v = (ve - 128)[:, :, None]
y = H[:, :, None]
rgb_yuyv = np.clip(np.dstack([y + 1.402 * v,
                              y - 0.344136 * u - 0.714136 * v,
                              y + 1.772 * u]), 0, 255).astype(np.uint8)
print("YUYV laplacian %.2f" % lapstd(rgb_yuyv.mean(axis=2)))
Image.fromarray(rgb_yuyv).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\fix_yuyv.png')

# ---- RGB565 baseline ----
hi = a[:, :, 0].astype(np.int64)
lo = a[:, :, 1].astype(np.int64)
rgb = np.dstack([hi & 0xF8, ((hi & 0x07) << 5) | ((lo & 0xE0) >> 3), (lo & 0x1F) << 3]).astype(np.uint8)
print("RGB565 laplacian %.2f" % lapstd(rgb.mean(axis=2)))
Image.fromarray(rgb).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\fix_rgb565.png')

print("\n--- smoothness of each plane under each hypothesis ---")
print("Y (H) horiz-adj   : %.2f   <- luma in YUYV / R+G in RGB565" % np.abs(H[:, 1:] - H[:, :-1]).mean())
print("L even (U) adj    : %.2f   <- chroma in YUYV" % np.abs(L[:, 0::2][:, 1:] - L[:, 0::2][:, :-1]).mean())
print("L odd  (V) adj    : %.2f   <- chroma in YUYV" % np.abs(L[:, 1::2][:, 1:] - L[:, 1::2][:, :-1]).mean())
print("lo whole adj      : %.2f   <- B+Glo in RGB565" % np.abs(L[:, 1:] - L[:, :-1]).mean())

print("\n--- 2x2 chroma block check: is U constant within a pixel pair? ---")
# in YUYV, consecutive pixel pairs (0,1),(2,3) share chroma, so
# pixel value at even x and odd x have IDENTICAL chroma -> color difference
# between x=2k and x=2k+1 is purely from Y.
for name, ch in (("U", L[:, 0::2]), ("V", L[:, 1::2])):
    print("   %s: even-col mean %.1f  odd-col mean %.1f  (same if YUYV)" % (name, ch[:, 0::2].mean(), ch[:, 1::2].mean()))
