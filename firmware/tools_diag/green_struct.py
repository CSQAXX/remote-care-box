import numpy as np, glob

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
A0 = f0[:,:,0].astype(np.int64); B0 = f0[:,:,1].astype(np.int64)
A1 = f1[:,:,0].astype(np.int64); B1 = f1[:,:,1].astype(np.int64)

def cc(a, b):
    a = a.ravel().astype(np.float64); b = b.ravel().astype(np.float64)
    a -= a.mean(); b -= b.mean()
    d = np.sqrt((a*a).sum()*(b*b).sum())
    return float((a*b).sum()/d) if d else 0.0

print("=== IS THE DATA YUV422 (not RGB565)? ===")
print("  If YUV422: A = Y (luma, 1 byte/pixel, SMOOTH), B = chroma (garbage here).")
print("  If RGB565: all 3 channels valid.")
print()
R5 = (A0 >> 3); G3 = (A0 & 7); G2 = (B0 >> 5); B5 = (B0 & 0x1F)
print("  A as Y (luma):  spatial adj %.2f   temporal corr %+.3f" % (
    np.abs(A0[:,1:]-A0[:,:-1]).mean(), cc(A0, A1)))
print("  B as chroma:    spatial adj %.2f   temporal corr %+.3f" % (
    np.abs(B0[:,1:]-B0[:,:-1]).mean(), cc(B0, B1)))
print()
print("  DECISIVE: in real YUV422, chroma is SUBSAMPLED (2 pixels share 1 chroma).")
print("  Test: is B[2k] == B[2k+1]? (would mean one chroma byte per 2 pixels)")
eq = (B0[:, 0::2] == B0[:, 1::2])
print("    fraction of equal (B_even, B_odd) pairs: %.4f" % eq.mean())
print("    (random would be ~0.004; high => shared chroma)")

print("\n=== but B&0x1F (low 5 bits of B) correlates %+.3f — why? ===" % cc(B0 & 0x1F, B1 & 0x1F))
print("  Check: is B&0x1F really stable, or is it just a small-range value?")
print("    B&0x1F std %.2f, mean %.2f" % ((B0 & 0x1F).std(), (B0 & 0x1F).mean()))
print("    B>>5   std %.2f, mean %.2f" % ((B0 >> 5).std(), (B0 >> 5).mean()))
print("  -> if B>>5 (3 bits, values 0-7) has similar std to B&0x1F,")
print("     and only the top bits are garbage, that's an RGB565-like pattern.")

print("\n=== GREEN-ONLY test: is green corrupted while R and B are fine? ===")
print("  R5 (A>>3)   spatial %.2f  temporal %+.3f" % (np.abs(R5[:,1:]-R5[:,:-1]).mean(), cc(R5, A1>>3)))
print("  B5 (B&0x1F) spatial %.2f  temporal %+.3f" % (np.abs(B5[:,1:]-B5[:,:-1]).mean(), cc(B5, B1&0x1F)))
print("  G3 (A&7)    spatial %.2f  temporal %+.3f" % (np.abs(G3[:,1:]-G3[:,:-1]).mean(), cc(G3, A1&7)))
print("  G2 (B>>5)   spatial %.2f  temporal %+.3f" % (np.abs(G2[:,1:]-G2[:,:-1]).mean(), cc(G2, B1>>5)))
print()
print("  >> ONLY the 6 green bits are unstable. R and B are clean.")
print("  >> This is the fingerprint of a MISALIGNED CHROMA/green source.")

print("\n=== TEST: are the green bits shifted by one pixel? ===")
G0 = ((A0 & 7) << 3) | ((B0 >> 5) & 7)
G1 = ((A1 & 7) << 3) | ((B1 >> 5) & 7)
for off in (-2, -1, 0, 1, 2):
    print("   green shift %+d: temporal corr %+.3f" % (off, cc(G0, np.roll(G1, off, axis=1))))

print("\n=== TEST: are the green bits shifted by one ROW? ===")
for off in (-2, -1, 0, 1, 2):
    print("   green row shift %+d: temporal corr %+.3f" % (off, cc(G0, np.roll(G1, off, axis=0))))

print("\n=== TEST: green from the PREVIOUS frame (1-frame lag)? ===")
print("   corr(G_f0, G_f1) = %+.3f" % cc(G0, G1))
print("   corr(G_f0, G_f0) = %+.3f  (sanity, should be 1.0)" % cc(G0, G0))
