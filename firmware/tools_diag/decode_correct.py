import numpy as np, glob
from PIL import Image

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
d = open(fs[0], 'rb').read()
i = d.find(b'\r\n\r\n')
b = d[i + 4:i + 4 + 153600]
a = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2)
H = a[:, :, 0].astype(np.float64)   # even bytes
L = a[:, :, 1].astype(np.float64)   # odd bytes

def lapstd(g):
    g = np.asarray(g, dtype=np.float64)
    return float((g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1]
                  - g[1:-1, :-2] - g[1:-1, 2:]).std())

# YUYV byte order: Y0 U Y1 V Y2 U Y3 V ...
# byte index: 0=Y0 1=U 2=Y1 3=V 4=Y2 5=U 6=Y3 7=V
# even bytes (H) = Y0,Y1,Y2,Y3  -> H[:, 0::2]
# odd bytes (L)  = U,V,U,V     -> U = L[:,0::4], V = L[:,1::4]
Y = H[:, 0::2]
U = L[:, 0::4]
V = L[:, 1::4]
print("Y shape", Y.shape, "U", U.shape, "V", V.shape)

u = np.repeat((U - 128), 2, axis=1)[:, :, None]
v = np.repeat((V - 128), 2, axis=1)[:, :, None]
yy = Y[:, :, None]
rgb_yuyv = np.clip(np.dstack([yy + 1.402 * v,
                              yy - 0.344136 * u - 0.714136 * v,
                              yy + 1.772 * u]), 0, 255).astype(np.uint8)
print("YUYV laplacian %.2f" % lapstd(rgb_yuyv.mean(axis=2)))
Image.fromarray(rgb_yuyv).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\dec_yuyv_correct.png')

# UYVY byte order: U Y0 V Y1
# 0=U 1=Y0 2=V 3=Y1 ...
Yu = L[:, 0::2]
Uu = H[:, 0::4]
Vu = H[:, 1::4]
u2 = np.repeat((Uu - 128), 2, axis=1)[:, :, None]
v2 = np.repeat((Vu - 128), 2, axis=1)[:, :, None]
yy2 = Yu[:, :, None]
rgb_uyvy = np.clip(np.dstack([yy2 + 1.402 * v2,
                              yy2 - 0.344136 * u2 - 0.714136 * v2,
                              yy2 + 1.772 * u2]), 0, 255).astype(np.uint8)
print("UYVY laplacian %.2f" % lapstd(rgb_uyvy.mean(axis=2)))
Image.fromarray(rgb_uyvy).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\dec_uyvy_correct.png')

# RGB565 baseline
hi = a[:, :, 0].astype(np.int64)
lo = a[:, :, 1].astype(np.int64)
rgb = np.dstack([hi & 0xF8, ((hi & 0x07) << 5) | ((lo & 0xE0) >> 3), (lo & 0x1F) << 3]).astype(np.uint8)
print("RGB565 laplacian %.2f" % lapstd(rgb.mean(axis=2)))
Image.fromarray(rgb).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\dec_rgb565.png')

print("\n--- chroma smoothness check (YUYV assumption) ---")
print("U horiz-adj diff %.2f  V horiz-adj diff %.2f" % (np.abs(U[:, 1:] - U[:, :-1]).mean(), np.abs(V[:, 1:] - V[:, :-1]).mean()))
print("Y horiz-adj diff %.2f" % np.abs(Y[:, 1:] - Y[:, :-1]).mean())
print("\n--- and check L[:,0::2] vs L[:,1::2] (interleaved chroma) ---")
print("L even-pos adj %.2f   L odd-pos adj %.2f" % (np.abs(L[:, 0::2][:, 1:] - L[:, 0::2][:, :-1]).mean(),
                                                    np.abs(L[:, 1::2][:, 1:] - L[:, 1::2][:, :-1]).mean()))
