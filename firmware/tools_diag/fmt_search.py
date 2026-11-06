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
NB = 240*320*2

def cc(a, b):
    a = a.ravel().astype(np.float64); b = b.ravel().astype(np.float64)
    a -= a.mean(); b -= b.mean()
    d = np.sqrt((a*a).sum()*(b*b).sum())
    return float((a*b).sum()/d) if d else 0.0

def adjh(P):
    P = P.reshape(240, -1) if P.ndim == 1 else P
    return float(np.abs(P[:,1:]-P[:,:-1]).mean())

b0 = np.unpackbits(f0); b1 = np.unpackbits(f1)

def planes(x, boff):
    """x: byte array; returns (A,B) int arrays of length 240*320"""
    v = x[boff:boff+NB]
    v = v[:(len(v)//2)*2]
    p = v.reshape(-1, 2)
    return p[:,0].astype(np.int64), p[:,1].astype(np.int64)

# ---------- PLAN 1: RGB565, word = (A<<8)|B ----------
print("=== PLAN 1: RGB565 word=(A<<8)|B ===")
best = []
for shift in range(16):
    s0 = np.packbits(np.roll(b0, shift)); s1 = np.packbits(np.roll(b1, shift))
    for boff in range(4):
        A0,B0 = planes(s0,boff); A1,B1 = planes(s1,boff)
        cR = cc(A0>>3, A1>>3)
        cG = cc(((A0&7)<<3)|((B0>>5)&7), ((A1&7)<<3)|((B1>>5)&7))
        cB = cc(B0&0x1F, B1&0x1F)
        best.append((min(cR,cG,cB), shift, boff, cR, cG, cB))
best.sort(reverse=True)
print("  minCorr shift boff     cR      cG      cB")
for m,s,bo,cR,cG,cB in best[:6]:
    print("   %+.3f    %2d    %d    %+.3f  %+.3f  %+.3f" % (m,s,bo,cR,cG,cB))

# ---------- PLAN 2: YUYV (Y=A, U=B[even], V=B[odd]) ----------
print("\n=== PLAN 2: YUYV  Y=A, U=B[even], V=B[odd] ===")
best = []
for shift in range(16):
    s0 = np.packbits(np.roll(b0, shift)); s1 = np.packbits(np.roll(b1, shift))
    for boff in range(4):
        A0,B0 = planes(s0,boff); A1,B1 = planes(s1,boff)
        cY = cc(A0, A1)
        cU = cc(B0[0::2], B1[0::2]); cV = cc(B0[1::2], B1[1::2])
        best.append((min(cY,cU,cV), shift, boff, cY, cU, cV))
best.sort(reverse=True)
print("  minCorr shift boff     cY      cU      cV")
for m,s,bo,cY,cU,cV in best[:6]:
    print("   %+.3f    %2d    %d    %+.3f  %+.3f  %+.3f" % (m,s,bo,cY,cU,cV))

# ---------- PLAN 3: UYVY (U=A[even], Y=B, V=A[odd]) ----------
print("\n=== PLAN 3: UYVY  U=A[even], Y=B, V=A[odd] ===")
best = []
for shift in range(16):
    s0 = np.packbits(np.roll(b0, shift)); s1 = np.packbits(np.roll(b1, shift))
    for boff in range(4):
        A0,B0 = planes(s0,boff); A1,B1 = planes(s1,boff)
        cY = cc(B0, B1)
        cU = cc(A0[0::2], A1[0::2]); cV = cc(A0[1::2], A1[1::2])
        best.append((min(cY,cU,cV), shift, boff, cY, cU, cV))
best.sort(reverse=True)
print("  minCorr shift boff     cY      cU      cV")
for m,s,bo,cY,cU,cV in best[:6]:
    print("   %+.3f    %2d    %d    %+.3f  %+.3f  %+.3f" % (m,s,bo,cY,cU,cV))

# ---------- PLAN 4: RGB565 word=(B<<8)|A ----------
print("\n=== PLAN 4: RGB565 word=(B<<8)|A (swapped bytes) ===")
best = []
for shift in range(16):
    s0 = np.packbits(np.roll(b0, shift)); s1 = np.packbits(np.roll(b1, shift))
    for boff in range(4):
        A0,B0 = planes(s0,boff); A1,B1 = planes(s1,boff)
        cR = cc(B0>>3, B1>>3)
        cG = cc(((B0&7)<<3)|((A0>>5)&7), ((B1&7)<<3)|((A1>>5)&7))
        cB = cc(A0&0x1F, A1&0x1F)
        best.append((min(cR,cG,cB), shift, boff, cR, cG, cB))
best.sort(reverse=True)
print("  minCorr shift boff     cR      cG      cB")
for m,s,bo,cR,cG,cB in best[:6]:
    print("   %+.3f    %2d    %d    %+.3f  %+.3f  %+.3f" % (m,s,bo,cR,cG,cB))
