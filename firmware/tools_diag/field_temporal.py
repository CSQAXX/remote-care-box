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
    return out

fr = load(r'D:\Software\kanhu\remote-care-box\raw3_*.bin')
f0, f1 = fr[0], fr[1]

def cc(a, b):
    a = a.ravel().astype(np.float64); b = b.ravel().astype(np.float64)
    a -= a.mean(); b -= b.mean()
    d = np.sqrt((a*a).sum()*(b*b).sum())
    return float((a*b).sum()/d) if d else 0.0

def adjh(P): return float(np.abs(P[:,1:]-P[:,:-1]).mean())
def adjv(P): return float(np.abs(P[1:]-P[:-1]).mean())

A0 = f0[:,:,0].astype(np.int64); A1 = f1[:,:,0].astype(np.int64)
B0 = f0[:,:,1].astype(np.int64); B1 = f1[:,:,1].astype(np.int64)

fields = {
    "A(raw byte)": (A0, A1),
    "A>>3  (R5) ": (A0>>3, A1>>3),
    "A&7   (G3) ": (A0&7,  A1&7),
    "B(raw byte)": (B0, B1),
    "B>>5  (G2) ": (B0>>5, B1>>5),
    "B&0x1F(B5) ": (B0&0x1F, B1&0x1F),
}

print("=== RGB565 hypothesis (big-endian bytes) ===")
print("field          temp_corr   adjH(f0)  adjV(f0)   |d| f0-f1")
for k, (p0, p1) in fields.items():
    print("  %s  %+.3f      %6.2f    %6.2f     %6.2f" % (
        k, cc(p0, p1), adjh(p0), adjv(p0), float(np.abs(p0-p1).mean())))

print("\n=== YUYV hypothesis ===")
Y0 = A0; Y1 = A1
U0 = B0[:, 0::2]; U1 = B1[:, 0::2]
V0 = B0[:, 1::2]; V1 = B1[:, 1::2]
for nm, (p0, p1) in {"Y": (Y0,Y1), "U": (U0,U1), "V": (V0,V1)}.items():
    print("  %s  temp %+.3f  adjH %6.2f  adjV %6.2f  |d| %6.2f" % (
        nm, cc(p0,p1), adjh(p0), adjv(p0), float(np.abs(p0-p1).mean())))

print("\n=== also: byte-swapped planes (A=LO-like, B=HI-like) ===")
# treat odd bytes as first byte of pixel
for nm, (p0, p1) in {"B>>3": (B0>>3, B1>>3), "B&7": (B0&7, B1&7),
                     "A>>5": (A0>>5, A1>>5), "A&0x1F": (A0&0x1F, A1&0x1F)}.items():
    print("  %-8s temp %+.3f  adjH %6.2f" % (nm, cc(p0,p1), adjh(p0)))

print("\n=== KEY: split B by whether high bits change ===")
print("  B>>5 change: %6.2f   B&0x1F change: %6.2f" % (
    float(np.abs((B0>>5)-(B1>>5)).mean()), float(np.abs((B0&0x1F)-(B1&0x1F)).mean())))
print("  A>>3 change: %6.2f   A&7   change: %6.2f" % (
    float(np.abs((A0>>3)-(A1>>3)).mean()), float(np.abs((A0&7)-(A1&7)).mean())))
