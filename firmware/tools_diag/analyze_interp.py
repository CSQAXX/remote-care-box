import numpy as np, glob
from PIL import Image

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
d = open(fs[0], 'rb').read()
i = d.find(b'\r\n\r\n')
b = d[i + 4:][:153600]
a = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2)
H = a[:, :, 0].astype(np.int64)
L = a[:, :, 1].astype(np.int64)

def lapstd(g):
    return float((g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1]
                  - g[1:-1, :-2] - g[1:-1, 2:]).std())

r = (H & 0xF8)
g = ((H & 0x07) << 5) | ((L & 0xE0) >> 3)
b_ = (L & 0x1F) << 3
rgbA = np.dstack([r, g, b_]).astype(np.uint8)
print("A shape", rgbA.shape)

u = L[:, 0::2]
v = L[:, 1::2]
y = H[:, 0::2]
U = (u - 128)[:, :, None]
V = (v - 128)[:, :, None]
rgbB = np.clip(np.dstack([y[:, :, None] + 1.402 * V,
                          y[:, :, None] - 0.344136 * U - 0.714136 * V,
                          y[:, :, None] + 1.772 * U]), 0, 255).astype(np.uint8)
print("B shape", rgbB.shape)

Image.fromarray(rgbA).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\interp_A_rgb565.png')
Image.fromarray(rgbB).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\interp_B_yuyv.png')

print("A laplacian %.2f | B laplacian %.2f" % (lapstd(rgbA.mean(axis=2)), lapstd(rgbB.mean(axis=2))))
print("U adj %.2f  V adj %.2f  hi adj %.2f" % (np.abs(u[:, 1:] - u[:, :-1]).mean(),
      np.abs(v[:, 1:] - v[:, :-1]).mean(), np.abs(H[:, 1:] - H[:, :-1]).mean()))
print("std lo-even %.1f  lo-odd %.1f" % (L[:, 0::2].std(), L[:, 1::2].std()))
print("hi row100:", H[100, 100:130].astype(int).tolist())
print("lo row100:", L[100, 100:130].astype(int).tolist())
