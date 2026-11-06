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
        out.append(np.frombuffer(body[:153600], dtype=np.uint8))
    return out

raw = load(r'D:\Software\kanhu\remote-care-box\raw3_*.bin')
f0, f1 = raw[0], raw[1]
N = 240*320

def cc(a, b):
    a = a.ravel().astype(np.float64); b = b.ravel().astype(np.float64)
    a -= a.mean(); b -= b.mean()
    d = np.sqrt((a*a).sum()*(b*b).sum())
    return float((a*b).sum()/d) if d else 0.0

print("=== PROOF: 'lo byte noise' is a bit-field artifact, not garbage ===")
A0 = f0[0::2].astype(np.int64); B0 = f0[1::2].astype(np.int64)
print("  raw hi byte adj diff      = %.2f" % np.abs(A0[1:]-A0[:-1]).mean())
print("  = 8 * R5diff(%.2f) + G3diff(%.2f) = %.2f" % (
    np.abs((A0>>3)[1:]-(A0>>3)[:-1]).mean(),
    np.abs((A0&7)[1:]-(A0&7)[:-1]).mean(),
    8*np.abs((A0>>3)[1:]-(A0>>3)[:-1]).mean() + np.abs((A0&7)[1:]-(A0&7)[:-1]).mean()))
print("  raw lo byte adj diff      = %.2f" % np.abs(B0[1:]-B0[:-1]).mean())
print("  = 32 * G2diff(%.2f)       = %.2f" % (
    np.abs((B0>>5)[1:]-(B0>>5)[:-1]).mean(),
    32*np.abs((B0>>5)[1:]-(B0>>5)[:-1]).mean()))
print("  -> the 'noise' is just 1-2 counts of the GREEN low bits.")

print("\n=== KEY PROBLEM: which channels are temporally stable? ===")
A1 = f1[0::2].astype(np.int64); B1 = f1[1::2].astype(np.int64)
print("  R5 = HI>>3      temp %+.3f   std(f0) %6.2f" % (cc(A0>>3, A1>>3), (A0>>3).std()))
print("  G3 = HI&7       temp %+.3f   std(f0) %6.2f" % (cc(A0&7,  A1&7),  (A0&7).std()))
print("  G2 = LO>>5      temp %+.3f   std(f0) %6.2f" % (cc(B0>>5, B1>>5), (B0>>5).std()))
print("  B5 = LO&0x1F    temp %+.3f   std(f0) %6.2f" % (cc(B0&0x1F, B1&0x1F), (B0&0x1F).std()))

print("\n  G3 per-frame stats:")
for nm, A in (("f0", A0), ("f1", A1)):
    g = A & 7
    print("    %s: mean %.3f std %.3f  hist %s" % (nm, g.mean(), g.std(),
          np.bincount(g.ravel(), minlength=8).tolist()))
print("  G2 per-frame stats:")
for nm, B in (("f0", B0), ("f1", B1)):
    g = B >> 5
    print("    %s: mean %.3f std %.3f  hist %s" % (nm, g.mean(), g.std(),
          np.bincount(g.ravel(), minlength=8).tolist()))

print("\n=== SEARCH: is there ANY bit alignment where all 3 channels are stable? ===")
b0 = np.unpackbits(f0); b1 = np.unpackbits(f1)
best = []
for shift in range(16):
    s0 = np.packbits(np.roll(b0, shift))
    s1 = np.packbits(np.roll(b1, shift))
    for boff in range(4):
        x0 = s0[boff:]; x1 = s1[boff:]
        n = N*2
        A0x = x0[:n].reshape(240,320,2)[:,:,0].astype(np.int64)
        A1x = x1[:n].reshape(240,320,2)[:,:,0].astype(np.int64)
        B0x = x0[:n].reshape(240,320,2)[:,:,1].astype(np.int64)
        B1x = x1[:n].reshape(240,320,2)[:,:,1].astype(np.int64)
        cR = cc(A0x>>3, A1x>>3)
        cG = cc((A0x&7)*8 + (B0x>>5), (A1x&7)*8 + (B1x>>5))
        cB = cc(B0x&0x1F, B1x&0x1F)
        best.append((min(cR,cG,cB), shift, boff, cR, cG, cB))
best.sort(reverse=True)
print("  minCorr shift boff     cR      cG      cB")
for m,s,bo,cR,cG,cB in best[:8]:
    print("   %+.3f    %2d    %d    %+.3f  %+.3f  %+.3f" % (m,s,bo,cR,cG,cB))
print("  current (shift0,boff0):")
cR=cc(A0>>3,A1>>3); cG=cc((A0&7)*8+(B0>>5),(A1&7)*8+(B1>>5)); cB=cc(B0&0x1F,B1&0x1F)
print("   min %+.3f  cR %+.3f cG %+.3f cB %+.3f" % (min(cR,cG,cB), cR, cG, cB))

print("\n=== YUYV check: separate even/odd positions of LO ===")
U0 = B0[0::2]; V0 = B0[1::2]
U1 = B1[0::2]; V1 = B1[1::2]
print("  U(even) temp %+.3f  V(odd) temp %+.3f" % (cc(U0,U1), cc(V0,V1)))
print("  U&0x1F temp %+.3f  V&0x1F temp %+.3f" % (cc(U0&0x1F, U1&0x1F), cc(V0&0x1F, V1&0x1F)))
print("  -> if these are ~0.5 too, LO is NOT chroma; it's RGB565 low byte.")
