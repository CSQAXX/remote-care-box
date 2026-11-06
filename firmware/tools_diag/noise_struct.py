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
A = b0[:, :, 0].astype(np.int64)
B = b0[:, :, 1].astype(np.int64)

R = ((A >> 3) & 0x1F) << 3
G = (((A & 7) << 3) | ((B >> 5) & 0x07))
Bl = (B & 0x1F) << 3
rgb = np.dstack([R, G, Bl])

print("=== is the noise periodic in X (bit-alignment) or spatial-random? ===")
for nm, plane in (("A", A), ("B", B)):
    # autocorrelation of horizontal differences at lags
    d = np.diff(plane.astype(np.float64), axis=1)
    d = d - d.mean()
    print("  plane %s: lag1 %.3f lag2 %.3f lag3 %.3f lag4 %.3f" % (
        nm,
        float((d[:,:-1]*d[:,1:]).sum()/(d*d).sum()),
        float((d[:,:-2]*d[:,2:]).sum()/(d*d).sum()),
        float((d[:,:-3]*d[:,3:]).sum()/(d*d).sum()),
        float((d[:,:-4]*d[:,4:]).sum()/(d*d).sum())))

print("\n=== noise map |f0-f1| : structured or random? ===")
if len(fr) >= 2:
    d = np.abs(fr[0].astype(np.int64) - fr[1].astype(np.int64)).mean(axis=2)
    print("  diff mean %.1f  row-to-row corr of diff: %.3f" % (
        d.mean(), np.corrcoef(d[:-1].ravel(), d[1:].ravel())[0,1]))
    # column profile of diff
    print("  col profile of diff [::20]:", [int(x) for x in d.mean(axis=0)[::20]])
    print("  row profile of diff [::20]:", [int(x) for x in d.mean(axis=1)[::20]])
    Image.fromarray((np.clip(d,0,255)).astype(np.uint8)).resize((960,720), Image.NEAREST).save(
        r'D:\Software\kanhu\remote-care-box\diffmap.png')

print("\n=== brightness-normalized decoded A ===")
m = rgb.mean(axis=2)
lo, hi = np.percentile(m, [1, 99])
print("  percentiles: %.0f .. %.0f" % (lo, hi))
norm = np.clip((m - lo) / max(hi-lo, 1) * 255, 0, 255).astype(np.uint8)
Image.fromarray(norm).resize((960,720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\norm.png')

print("\n=== zoom crop of decoded (rows 100-140, cols 120-180) ===")
crop = rgb[100:140, 120:180]
Image.fromarray(crop.astype(np.uint8)).resize((720,480), Image.NEAREST).save(
    r'D:\Software\kanhu\remote-care-box\crop.png')

print("\n=== unique-value check: how many distinct values per channel? ===")
for i, c in enumerate("RGB"):
    u = np.unique(rgb[:,:,i])
    print("  %s: %d distinct, range %d..%d" % (c, len(u), u.min(), u.max()))
