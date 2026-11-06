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
b0 = fr[0]
HI = b0[:, :, 0].astype(np.int64)
LO = b0[:, :, 1].astype(np.int64)

R5 = HI >> 3
G3 = HI & 7
G2 = LO >> 5
B5 = LO & 0x1F

def corr(a, b):
    a = a.ravel() - a.mean(); b = b.ravel() - b.mean()
    d = np.sqrt((a*a).sum() * (b*b).sum())
    return float((a*b).sum()/d) if d else 0.0

print("=== LATERAL misalignment: R5[x] vs B5[x+k] ===")
for k in range(-4, 5):
    if k >= 0:
        a = R5[:, :-k or None]; b = B5[:, k:]
    else:
        a = R5[:, -k:]; b = B5[:, :k]
    print("  k=%+d  corr %.3f" % (k, corr(a, b)))

print("\n=== VERTICAL misalignment: R5[r] vs B5[r+m] ===")
for m in range(-4, 5):
    if m >= 0:
        a = R5[m:]; b = B5[:240-m]
    else:
        a = R5[:240+m]; b = B5[-m:]
    print("  m=%+d  corr %.3f" % (m, corr(a, b)))

print("\n=== row-mean correlation HI vs LO (which row does LO[r] match?) ===")
rh = HI.mean(axis=1); rl = LO.mean(axis=1)
print("  HI row means [:12]:", [int(x) for x in rh[:12]])
print("  LO row means [:12]:", [int(x) for x in rl[:12]])
print("  corr(HI[r], LO[r])   = %.3f" % corr(rh, rl))
for m in range(1, 4):
    print("  corr(HI[r], LO[r%+d]) = %.3f" % (m, corr(rh[:240-m], rl[m:])))

print("\n=== within a row: is HI row r's mean consistent with LO row r? ===")
print("  HI row means [::20]:", [int(x) for x in rh[::20]])
print("  LO row means [::20]:", [int(x) for x in rl[::20]])

print("\n=== Is LO actually noise, or just a DIFFERENT image channel? ===")
print("  corr(LO, LO) self = %.3f" % corr(LO, LO))
print("  std HI %.1f  std LO %.1f" % (HI.std(), LO.std()))
print("  LO value histogram (top bins):")
h, e = np.histogram(LO, bins=16, range=(0, 256))
for i in range(16):
    print("    %3d-%3d: %s" % (e[i], e[i+1], '#'*int(60*h[i]/h.max())))

print("\n=== HI histogram ===")
h2, e2 = np.histogram(HI, bins=16, range=(0, 256))
for i in range(16):
    print("    %3d-%3d: %s" % (e2[i], e2[i+1], '#'*int(60*h2[i]/h2.max())))

Image.fromarray(b0).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\raw_bytes.png')
print("\nsaved raw_bytes.png")
