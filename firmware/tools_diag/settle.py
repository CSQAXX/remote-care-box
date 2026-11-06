import numpy as np, glob
from PIL import Image

f = r'D:\Software\kanhu\remote-care-box\raw3_1.bin'
d = open(f, 'rb').read()
i = d.find(b'\r\n\r\n')
buf = d[i + 4:i + 4 + 153600]
a = np.frombuffer(buf, dtype=np.uint8).reshape(240, 320, 2)

HI = a[:, :, 0].astype(np.int64)   # even bytes
LO = a[:, :, 1].astype(np.int64)   # odd bytes

def adj(P):
    return float(np.abs(P[:, 1:] - P[:, :-1]).mean())

def corrx(P):
    return float(np.corrcoef(P[:, :-1].ravel(), P[:, 1:].ravel())[0, 1])

def corry(P):
    return float(np.corrcoef(P[:-1].ravel(), P[1:].ravel())[0, 1])

print("=== planes ===")
print("HI  adj %6.2f  corrX %.3f  corrY %.3f" % (adj(HI), corrx(HI), corry(HI)))
print("LO  adj %6.2f  corrX %.3f  corrY %.3f" % (adj(LO), corrx(LO), corry(LO)))

print("\n=== ACTUAL VALUES: row 120, cols 100-116 ===")
print("HI:", HI[120, 100:116].tolist())
print("LO:", LO[120, 100:116].tolist())
print("\n=== ACTUAL VALUES: col 150, rows 100-116 ===")
print("HI:", HI[100:116, 150].tolist())
print("LO:", LO[100:116, 150].tolist())

print("\n=== HI[120] first differences over cols 100-116 ===")
print(np.diff(HI[120, 100:116]).tolist())

print("\n=== compare: a SMOOTH synthetic ramp for reference ===")
ramp = (np.arange(320) // 4).astype(np.int64)[None, :]
print("ramp adj %.2f corrX %.3f" % (adj(ramp), corrx(ramp)))

print("\n=== mean per row (HI) ===")
print([int(x) for x in HI.mean(axis=1)[::20]])
print("=== mean per row (LO) ===")
print([int(x) for x in LO.mean(axis=1)[::20]])

Image.fromarray(HI.astype(np.uint8)).resize((640, 480), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\s_hi.png')
Image.fromarray(LO.astype(np.uint8)).resize((640, 480), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\s_lo.png')
