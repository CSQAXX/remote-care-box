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
A = b0[:, :, 0].astype(np.int64)   # even bytes
B = b0[:, :, 1].astype(np.int64)   # odd bytes

def corr(a, b):
    a = a.ravel().astype(np.float64); b = b.ravel().astype(np.float64)
    a -= a.mean(); b -= b.mean()
    return float((a*b).sum()/np.sqrt((a*a).sum()*(b*b).sum()))

def nat(img):
    """natural-image score: high local structure, low high-freq noise"""
    g = img.mean(axis=2).astype(np.float64)
    lap = g[1:-1,1:-1]*4 - g[:-2,1:-1] - g[2:,1:-1] - g[1:-1,:-2] - g[1:-1,2:]
    return float(lap.std()), float(np.abs(np.diff(g,axis=1)).mean())

def save(img, name):
    Image.fromarray(img.astype(np.uint8)).resize((960,720), Image.NEAREST).save(
        r'D:\Software\kanhu\remote-care-box\%s.png' % name)

# --- Decode A: byte0=high(RRRRRGGG), byte1=low(GGGBBBBB)  big-endian RGB565
RA = A & 0xF8
GA = ((A & 7) << 5) | ((B & 0xE0) >> 3)
BA = (B & 0x1F) << 3
imgA = np.dstack([RA, GA, BA])

# --- Decode B: byte0=low(GGGBBBBB), byte1=high(RRRRRGGG)  little-endian RGB565
RB = B & 0xF8
GB = ((B & 7) << 5) | ((A & 0xE0) >> 3)
BB = (A & 0x1F) << 3
imgB = np.dstack([RB, GB, BB])

for nm, img in (("A", imgA), ("B", imgB)):
    lap, d1 = nat(img)
    print("Decode %s: mean RGB %s  lap %.1f  adjdiff %.2f  corr(R,B) %.3f" % (
        nm, np.round(img.mean(axis=(0,1)),1), lap, d1, corr(img[:,:,0], img[:,:,2])))

save(imgA, "decA"); save(imgB, "decB")
save(A.astype(np.uint8), "planeA"); save(B.astype(np.uint8), "planeB")

print("\n=== per-field adj diff (all candidates) ===")
def adj(P): return float(np.abs(P[:,1:]-P[:,:-1]).mean())
print("  A>>3 (R5 in A)      %.2f" % adj(A>>3))
print("  A&7  (G3 in A)      %.2f" % adj(A&7))
print("  B>>5 (G2 in A)      %.2f" % adj(B>>5))
print("  B&0x1F (B5 in A)    %.2f" % adj(B&0x1F))
print("  B>>3 (R5 in B)      %.2f" % adj(B>>3))
print("  B&7  (G3 in B)      %.2f" % adj(B&7))
print("  A>>5 (G2 in B)      %.2f" % adj(A>>5))
print("  A&0x1F (B5 in B)    %.2f" % adj(A&0x1F))

print("\n=== R vs B plane correlation for each pairing ===")
print("  A: corr(A>>3, B&0x1F) = %.3f" % corr(A>>3, B&0x1F))
print("  B: corr(B>>3, A&0x1F) = %.3f" % corr(B>>3, A&0x1F))

print("\n=== frame-to-frame per-field change ===")
if len(fr) >= 2:
    A1 = fr[1][:,:,0].astype(np.int64); B1 = fr[1][:,:,1].astype(np.int64)
    print("  |A0-A1| %.2f   |B0-B1| %.2f" % (np.abs(A-A1).mean(), np.abs(B-B1).mean()))
    print("  corr(A0,A1) %.3f  corr(B0,B1) %.3f" % (corr(A, A1), corr(B, B1)))
