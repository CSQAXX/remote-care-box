import numpy as np, glob
from PIL import Image

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
frames = []
for f in fs:
    d = open(f, 'rb').read()
    i = d.find(b'\r\n\r\n')
    frames.append(np.frombuffer(d[i + 4:i + 4 + 153600], dtype=np.uint8).reshape(240, 320, 2))
frames = np.array(frames)

H = frames[0, :, :, 0]
L = frames[0, :, :, 1]

Image.fromarray(H).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\plane_hi.png')
Image.fromarray(L).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\plane_lo.png')

print("hi plane stats: min %d max %d mean %.1f std %.1f" % (H.min(), H.max(), H.mean(), H.std()))
print("lo plane stats: min %d max %d mean %.1f std %.1f" % (L.min(), L.max(), L.mean(), L.std()))
print()
print("lo & 0x1F histogram (top):")
v, c = np.unique(L & 0x1F, return_counts=True)
for k in np.argsort(c)[::-1][:8]:
    print("   0x%02X x%d (%.1f%%)" % (v[k], c[k], 100.0 * c[k] / L.size))
print()
print("hi >> 3 histogram (top):")
v, c = np.unique(H >> 3, return_counts=True)
for k in np.argsort(c)[::-1][:8]:
    print("   %d x%d (%.1f%%)" % (v[k], c[k], 100.0 * c[k] / H.size))

# Is lo's low 5 bits constant per-region? Map it.
sat = (L & 0x1F) == 0x1F
print("\nfraction of saturated-blue pixels per 30-row band:")
for y in range(0, 240, 30):
    print("   rows %3d-%3d: %.2f" % (y, y + 29, sat[y:y + 30].mean()))
print("\nfraction of saturated-blue pixels per 40-col band:")
for x in range(0, 320, 40):
    print("   cols %3d-%3d: %.2f" % (x, x + 39, sat[:, x:x + 40].mean()))
