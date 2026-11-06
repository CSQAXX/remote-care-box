import numpy as np, glob

OUT = r'D:\Software\kanhu\remote-care-box'

def load():
    out = []
    for f in sorted(glob.glob(OUT + r'\raw3_*.bin')):
        d = open(f, 'rb').read()
        i = d.find(b'\r\n\r\n')
        body = d[i+4:]
        if len(body) < 153600:
            print("  %s TRUNCATED %d" % (f[-11:], len(body))); continue
        out.append(np.frombuffer(body[:153600], dtype=np.uint8).reshape(240, 320, 2))
    return out

fr = load()
print("frames:", len(fr))

def green_plane(f):
    A = f[:,:,0].astype(np.int64); B = f[:,:,1].astype(np.int64)
    return ((A & 7) << 3) | ((B >> 5) & 7)      # 0..63

def r5_plane(f):  return (f[:,:,0].astype(np.int64) >> 3)
def b5_plane(f):  return (f[:,:,1].astype(np.int64) & 0x1F)

def median_residual(P, k=2):
    """|P[x] - median(P[x-k..x-1], P[x+1..x+k])|  (center excluded)"""
    n = P.shape[1]
    res = np.zeros_like(P)
    for x in range(k, n - k):
        nb = [P[:, x-d] for d in range(1, k+1)] + [P[:, x+d] for d in range(1, k+1)]
        res[:, x] = np.abs(P[:, x] - np.median(np.stack(nb), axis=0))
    return res

def report(f, name):
    G = green_plane(f); R = r5_plane(f); B = b5_plane(f)
    rg = median_residual(G)
    rr = median_residual(R)
    rb = median_residual(B)
    print("\n--- %s ---" % name)
    for tag, res, scale in (("GREEN(0-63)", rg, 1), ("R5(0-31)", rr, 1), ("B5(0-31)", rb, 1)):
        for thr in (2, 3, 4):
            frac = (res > thr).mean()
            print("   %-12s residual > %d : %6.3f%% of pixels" % (tag, thr, frac*100))
    print("   GREEN residual: mean %.3f  p50 %.1f  p90 %.1f  p99 %.1f  max %d" % (
        rg[:, 2:-2].mean(), np.percentile(rg[:,2:-2],50), np.percentile(rg[:,2:-2],90),
        np.percentile(rg[:,2:-2],99), rg.max()))

report(fr[0], "frame 0")
if len(fr) > 1:
    report(fr[1], "frame 1")

print("\n\n=== BASELINE on a SYNTHETIC CLEAN image (what 'clean' should look like) ===")
h, w = 240, 320
yy, xx = np.mgrid[0:h, 0:w]
A_s = ((yy//4 * 8 + xx//8) & 0xF8).astype(np.uint8)
B_s = ((xx//16 * 4) & 0xFC).astype(np.uint8)
clean = np.dstack([A_s, B_s])
G = green_plane(clean); R = r5_plane(clean); B = b5_plane(clean)
rg = median_residual(G)
print("   GREEN residual: mean %.3f  p99 %.1f  max %d" % (rg[:,2:-2].mean(), np.percentile(rg[:,2:-2],99), rg.max()))
for thr in (2,3,4):
    print("   GREEN residual > %d : %.3f%%" % (thr, (rg[:,2:-2] > thr).mean()*100))

print("\n=== BAD-ROW rule: fraction of pixels with GREEN residual > 3 per row ===")
for f, nm in ((fr[0], "frame0"), (fr[1], "frame1") if len(fr)>1 else (fr[0], "frame1")):
    G = green_plane(f)
    rg = median_residual(G)
    frac = (rg[:, 2:-2] > 3).mean(axis=1)
    print("   %s: rows with frac>2%%: %d/240   frac range %.4f..%.4f  median %.4f" % (
        nm, (frac > 0.02).sum(), frac.min(), frac.max(), np.median(frac)))
