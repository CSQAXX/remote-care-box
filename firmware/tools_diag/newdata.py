import numpy as np, glob
from PIL import Image

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw3_*.bin'))
frames = []
for f in fs:
    d = open(f, 'rb').read()
    i = d.find(b'\r\n\r\n')
    body = d[i + 4:]
    if len(body) < 153600:
        print("SKIP (short %d):" % len(body), f)
        continue
    frames.append(np.frombuffer(body[:153600], dtype=np.uint8).reshape(240, 320, 2))
frames = np.array(frames)
print("usable frames:", len(frames))

H = frames[:, :, :, 0].astype(np.float64)
L = frames[:, :, :, 1].astype(np.float64)

print("\n=== correlation structure ===")
for name, P in (("HI (even bytes)", H), ("LO (odd bytes)", L)):
    hx = np.corrcoef(P[0][:, :-1].ravel(), P[0][:, 1:].ravel())[0, 1]
    hy = np.corrcoef(P[0][:-1].ravel(), P[0][1:].ravel())[0, 1]
    print("  %s: horiz %.3f  vert %.3f" % (name, hx, hy))

print("\n=== temporal stability per byte plane (frame0 vs frame1) ===")
for name, P in (("HI", H), ("LO", L)):
    d = np.abs(P[1] - P[0])
    print("  %s: bytes changed %.1f%%  mean|d| %.1f" % (name, 100 * (d > 0).mean(), d.mean()))

print("\n=== even/odd byte interleaving check ===")
# if the DVP is misaligned by 1 bit/byte, odd and even bytes would be related
print("  corr(HI[x], LO[x])   = %.3f" % np.corrcoef(H[0][:, :-1].ravel(), L[0][:, :-1].ravel())[0, 1])
print("  corr(HI[x], LO[x+1]) = %.3f" % np.corrcoef(H[0][:, :-1].ravel(), L[0][:, 1:].ravel())[0, 1])

print("\n=== what if the true pixel width is 8-bit and we should use only HI? ===")
hi8 = H[0]
Image.fromarray(hi8.astype(np.uint8)).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\new_hi_only.png')
Image.fromarray(L[0].astype(np.uint8)).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\new_lo_only.png')
print("saved new_hi_only.png / new_lo_only.png")

print("\n=== and the RGB565 decode for reference ===")
hi = frames[0, :, :, 0].astype(np.int64)
lo = frames[0, :, :, 1].astype(np.int64)
rgb = np.dstack([hi & 0xF8, ((hi & 0x07) << 5) | ((lo & 0xE0) >> 3), (lo & 0x1F) << 3]).astype(np.uint8)
Image.fromarray(rgb).resize((960, 720), Image.NEAREST).save(r'D:\Software\kanhu\remote-care-box\new_rgb565.png')
