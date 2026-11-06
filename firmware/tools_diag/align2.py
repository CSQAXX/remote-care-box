import numpy as np, glob

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

b0 = np.unpackbits(f0); b1 = np.unpackbits(f1)

print("=== EXHAUSTIVE alignment search: (bit_shift, byte_offset) ===")
print("  looking for an alignment where R, G, B all correlate > 0.45\n")
res = []
for shift in range(16):
    s0 = np.packbits(np.roll(b0, shift))
    s1 = np.packbits(np.roll(b1, shift))
    for boff in range(8):
        x0 = s0[boff:boff+NB]; x1 = s1[boff:boff+NB]
        A0 = x0.reshape(-1, 2)[:, 0].astype(np.int64)
        B0 = x0.reshape(-1, 2)[:, 1].astype(np.int64)
        A1 = x1.reshape(-1, 2)[:, 0].astype(np.int64)
        B1 = x1.reshape(-1, 2)[:, 1].astype(np.int64)
        cR = cc(A0 >> 3, A1 >> 3)
        cG = cc(((A0 & 7) << 3) | ((B0 >> 5) & 7), ((A1 & 7) << 3) | ((B1 >> 5) & 7))
        cB = cc(B0 & 0x1F, B1 & 0x1F)
        res.append((min(cR, cG, cB), shift, boff, cR, cG, cB))
res.sort(reverse=True)
print("  minCorr shift boff     cR      cG      cB")
for m, s, bo, cR, cG, cB in res[:12]:
    print("   %+.3f    %2d    %d    %+.3f  %+.3f  %+.3f" % (m, s, bo, cR, cG, cB))

print("\n  current alignment (shift 0, boff 0):")
A0 = f0.reshape(-1,2)[:,0].astype(np.int64); B0 = f0.reshape(-1,2)[:,1].astype(np.int64)
A1 = f1.reshape(-1,2)[:,0].astype(np.int64); B1 = f1.reshape(-1,2)[:,1].astype(np.int64)
cR = cc(A0>>3, A1>>3); cG = cc(((A0&7)<<3)|((B0>>5)&7), ((A1&7)<<3)|((B1>>5)&7)); cB = cc(B0&0x1F, B1&0x1F)
print("   cR %+.3f  cG %+.3f  cB %+.3f" % (cR, cG, cB))

print("\n=== Which bit positions are corrupted? (per-bit temp corr, current align) ===")
for bit in range(16):
    c = cc(b0[bit::16], b1[bit::16])
    tag = ""
    if bit in (0,1,2): tag = "  <- G3 of high byte"
    if bit in (8,9,10): tag = "  <- G2 of low byte"
    if bit in (3,4,5,6,7): tag = "  <- R5"
    if bit in (11,12,13,14,15): tag = "  <- B5"
    print("  bit %2d: corr %+.3f%s" % (bit, c, tag))

print("\n=== Hypothesis: GREEN bits come from a DIFFERENT pixel (1-pixel offset)? ===")
for off in range(-3, 4):
    # shift green contribution by off pixels
    G0 = ((A0 & 7) << 3) | ((B0 >> 5) & 7)
    G1 = ((A1 & 7) << 3) | ((B1 >> 5) & 7)
    G1s = np.roll(G1, off)
    print("  green shifted %+d: temp corr %+.3f" % (off, cc(G0, G1s)))

print("\n=== Hypothesis: whole-frame GREEN is from the PREVIOUS frame? ===")
G0 = ((A0 & 7) << 3) | ((B0 >> 5) & 7)
G1 = ((A1 & 7) << 3) | ((B1 >> 5) & 7)
print("  corr(G_f0, G_f1)      = %+.3f" % cc(G0, G1))
print("  corr(G_f0, G_f1+1row)= %+.3f" % cc(G0[:-1], G1[1:]))
print("  corr(G_f0, G_f0+1row)= %+.3f" % cc(G0[:-1], G0[1:]))
print("  corr(G_f0, G_f0[1:]) = %+.3f" % cc(G0[:-1], G0[1:]))
