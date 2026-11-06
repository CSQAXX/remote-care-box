import numpy as np, glob

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
frames = []
for f in fs:
    d = open(f, 'rb').read()
    i = d.find(b'\r\n\r\n')
    frames.append(np.frombuffer(d[i + 4:i + 4 + 153600], dtype=np.uint8).reshape(240, 320, 2))
frames = np.array(frames)

H = frames[:, :, :, 0].astype(np.float64)   # even bytes
L = frames[:, :, :, 1].astype(np.float64)   # odd bytes

print("=== correlations (frame 0) ===")
h0, l0 = H[0], L[0]
print("corr(hi, lo)          = %.3f" % np.corrcoef(h0.ravel(), l0.ravel())[0, 1])
print("corr(lo[x], hi[x+1])  = %.3f" % np.corrcoef(l0[:, :-1].ravel(), h0[:, 1:].ravel())[0, 1])
print("corr(lo[x], hi[x-1])  = %.3f" % np.corrcoef(l0[:, 1:].ravel(), h0[:, :-1].ravel())[0, 1])
print("corr(lo[x], hi[x])    = %.3f" % np.corrcoef(l0.ravel(), h0.ravel())[0, 1])

print("\n=== is lo noise random per-pixel, or structured? ===")
# temporal variance of each pixel's lo byte across frames
lo_var = L.std(axis=0)
print("lo temporal std: mean %.1f  min %.1f  max %.1f" % (lo_var.mean(), lo_var.min(), lo_var.max()))
hi_var = H.std(axis=0)
print("hi temporal std: mean %.1f  min %.1f  max %.1f" % (hi_var.mean(), hi_var.min(), hi_var.max()))

print("\n=== spatial autocorrelation of lo noise (frame0 vs mean) ===")
mean_img = frames.astype(np.float64).mean(axis=0)
resid = L[0] - mean_img[:, :, 1]
# neighbor correlation of residual
def corr(a, b):
    return np.corrcoef(a.ravel(), b.ravel())[0, 1]
print("corr(resid[x], resid[x+1]) = %.3f" % corr(resid[:, :-1], resid[:, 1:]))
print("corr(resid[x], resid[x+2]) = %.3f" % corr(resid[:, :-2], resid[:, 2:]))
print("corr(resid[x], resid[x+4]) = %.3f" % corr(resid[:, :-4], resid[:, 4:]))
print("corr(resid[x], resid[x+8]) = %.3f" % corr(resid[:, :-8], resid[:, 8:]))
print("corr(resid[x], resid[y+1]) = %.3f" % corr(resid[:-1], resid[1:]))

print("\n=== bit-level: how many bits flip? ===")
# compare frame0 lo bytes with frame1 lo bytes
d = np.abs(L[0].astype(int) - L[1].astype(int))
print("lo bytes differing between f0,f1: %.1f%%" % (100.0 * (d > 0).mean()))
dhi = np.abs(H[0].astype(int) - H[1].astype(int))
print("hi bytes differing between f0,f1: %.1f%%" % (100.0 * (dhi > 0).mean()))
# popcount of xor for the differing lo bytes
xor = (L[0].astype(np.uint8) ^ L[1].astype(np.uint8))
print("avg bit flips per differing lo byte: %.2f" % (np.unpackbits(xor.ravel()).sum() / max(1, (xor != 0).sum())))
xorh = (H[0].astype(np.uint8) ^ H[1].astype(np.uint8))
print("avg bit flips per differing hi byte: %.2f" % (np.unpackbits(xorh.ravel()).sum() / max(1, (xorh != 0).sum())))

print("\n=== which bits are unstable? (lo byte) ===")
unstable = np.unpackbits(xor.ravel()).reshape(-1, 8).mean(axis=0)
for i, v in enumerate(unstable):
    print("   bit %d: %.1f%% flips" % (i, 100 * v))
print("=== which bits are unstable? (hi byte) ===")
unstableh = np.unpackbits(xorh.ravel()).reshape(-1, 8).mean(axis=0)
for i, v in enumerate(unstableh):
    print("   bit %d: %.1f%% flips" % (i, 100 * v))
