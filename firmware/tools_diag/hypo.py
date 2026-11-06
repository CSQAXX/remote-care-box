import numpy as np, glob
from PIL import Image

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
d = open(fs[0], 'rb').read()
i = d.find(b'\r\n\r\n')
b = d[i + 4:i + 4 + 153600]
a = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2)
H = a[:, :, 0].astype(np.int64)
L = a[:, :, 1].astype(np.int64)

def lapstd(g):
    g = np.asarray(g, dtype=np.float64)
    return float((g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1]
                  - g[1:-1, :-2] - g[1:-1, 2:]).std())

print("=== Render each hypothesis as an image and save ===")
# 1) hi byte only, as 8-bit grayscale (320x240)
Image.fromarray(H.astype(np.uint8)).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\h1_hi_only.png')
# 2) lo byte only
Image.fromarray(L.astype(np.uint8)).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\h2_lo_only.png')

print("hi-only laplacian %.2f   lo-only laplacian %.2f" % (lapstd(H), lapstd(L)))

print("\n=== HYPOTHESIS: the real data is 8-bit, we're misreading 16-bit ===")
# if 8-bit: 153600 bytes = 640 x 240 of 8-bit gray
img8 = np.frombuffer(b, dtype=np.uint8).reshape(240, 640)
print("640x240 8-bit laplacian: %.2f" % lapstd(img8))
Image.fromarray(img8).resize((1280, 480), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\h3_as8bit.png')

print("\n=== HYPOTHESIS: byte order swapped (little-endian words) ===")
sw_h = L.copy(); sw_l = H.copy()
print("swapped: hi adj %.2f  lo adj %.2f" % (
    np.abs(sw_h[:, 1:] - sw_h[:, :-1]).mean(), np.abs(sw_l[:, 1:] - sw_l[:, :-1]).mean()))

print("\n=== HYPOTHESIS: the DVP captured each pixel's bytes from DIFFERENT pixels ===")
# i.e., half-pixel misalignment: L[x] actually belongs to pixel x+1
print("corr(L[x], H[x])   = %.3f" % np.corrcoef(L[:, :-1].ravel(), H[:, :-1].ravel())[0, 1])
print("corr(L[x], H[x+1]) = %.3f" % np.corrcoef(L[:, :-1].ravel(), H[:, 1:].ravel())[0, 1])
print("corr(L[x], H[x-1]) = %.3f" % np.corrcoef(L[:, 1:].ravel(), H[:, :-1].ravel())[0, 1])

print("\n=== HYPOTHESIS: sensor is in 2-byte mode but DVP captured only 8 bits ===")
# In that case, the "16-bit words" would be (byteN, byteN+1) where byteN is
# the real pixel and byteN+1 is garbage/noise.
print("corr(H[x], H[x+1]) = %.3f  (if hi is a real 8-bit image, should be high)" % np.corrcoef(H[:, :-1].ravel(), H[:, 1:].ravel())[0, 1])
print("corr(L[x], L[x+1]) = %.3f  (if lo is a real 8-bit image, should be high)" % np.corrcoef(L[:, :-1].ravel(), L[:, 1:].ravel())[0, 1])
print("corr(H[x], L[x])   = %.3f" % np.corrcoef(H[:, :-1].ravel(), L[:, :-1].ravel())[0, 1])

print("\n=== vertical correlation (same column, adjacent rows) ===")
print("corr(H[y], H[y+1]) = %.3f" % np.corrcoef(H[:-1].ravel(), H[1:].ravel())[0, 1])
print("corr(L[y], L[y+1]) = %.3f" % np.corrcoef(L[:-1].ravel(), L[1:].ravel())[0, 1])
