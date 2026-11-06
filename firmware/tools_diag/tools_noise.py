import numpy as np
from PIL import Image

W, H = 320, 240
data = open("raw_frame.bin", "rb").read()
arr = np.frombuffer(data, dtype=">u2").reshape(H, W).astype(np.int32)

def split(a):
    r5 = (a >> 11) & 0x1F
    g6 = (a >> 5) & 0x3F
    b5 = a & 0x1F
    return {"R5": r5, "G6": g6, "B5": b5,
            "G3": (a >> 8) & 0x07, "G2": (a >> 5) & 0x07}

ch = split(arr)

def med3(res):
    """3x3 median filter residual"""
    p = np.pad(res, 1, mode='edge')
    stack = np.stack([p[0:-2,0:-2], p[0:-2,1:-1], p[0:-2,2:],
                      p[1:-1,0:-2], p[1:-1,1:-1], p[1:-1,2:],
                      p[2:,0:-2],   p[2:,1:-1],   p[2:,2:]], axis=0)
    return np.abs(res - np.median(stack, axis=0))

print("=== 3x3 median-residual per channel (noise estimator) ===")
for name, v in ch.items():
    r = med3(v)
    print(f"  {name}: mean={r.mean():.4f}  p99={np.percentile(r,99):.1f}  max={r.max():.0f}  frac>1={100*(r>1).mean():.2f}%")

print()
print("=== horizontal 2nd-difference (isolated spike detector) ===")
for name, v in ch.items():
    d2 = v[:, 2:] - 2*v[:, 1:-1] + v[:, :-2]
    print(f"  {name}: mean|d2|={np.abs(d2).mean():.4f}  max|d2|={np.abs(d2).max():.0f}")

print()
print("=== spatial autocorrelation of each channel (lag 1) ===")
for name, v in ch.items():
    x = v[:, :-1].astype(np.float64).ravel()
    y = v[:, 1:].astype(np.float64).ravel()
    x -= x.mean(); y -= y.mean()
    ac = (x*y).sum() / np.sqrt((x*x).sum()*(y*y).sum())
    print(f"  {name}: ac1 = {ac:.4f}")

print()
print("=== vertical autocorrelation (lag 1) ===")
for name, v in ch.items():
    x = v[:-1, :].astype(np.float64).ravel()
    y = v[1:, :].astype(np.float64).ravel()
    x -= x.mean(); y -= y.mean()
    ac = (x*y).sum() / np.sqrt((x*x).sum()*(y*y).sum())
    print(f"  {name}: ac1 = {ac:.4f}")

# zoom crop of a flat-ish region
r = ((arr >> 11) & 0x1F) * 255 // 31
g = ((arr >> 5) & 0x3F) * 255 // 63
b = (arr & 0x1F) * 255 // 31
img = np.dstack([r, g, b]).astype(np.uint8)
Image.fromarray(img).resize((W*3, H*3), Image.NEAREST).save("raw_frame.png")
Image.fromarray(img[100:140, 150:190]).resize((40*10, 40*10), Image.NEAREST).save("raw_zoom.png")
print("saved raw_frame.png / raw_zoom.png")
