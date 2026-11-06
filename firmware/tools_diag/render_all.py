import numpy as np, glob
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'

def load():
    out = []
    for f in sorted(glob.glob(OUT + r'\raw3_*.bin')):
        d = open(f, 'rb').read()
        i = d.find(b'\r\n\r\n')
        body = d[i+4:]
        if len(body) < 153600:
            print("  %s TRUNCATED: %d / 153600" % (f.split('\\')[-1], len(body)))
            continue
        out.append(np.frombuffer(body[:153600], dtype=np.uint8).reshape(240, 320, 2))
    return out

fr = load()
f0 = fr[0]

def med3(img):
    p = np.pad(img, ((1,1),(1,1),(0,0)), mode='edge')
    st = [p[dy:dy+img.shape[0], dx:dx+img.shape[1]] for dy in range(3) for dx in range(3)]
    return np.median(np.stack(st), axis=0).astype(np.uint8)

# --- Interpretation 1: RGB565 big-endian, hi=A, lo=B ---
A = f0[:,:,0].astype(np.int64); B = f0[:,:,1].astype(np.int64)
rgb = np.dstack([ ((A>>3)&0x1F)<<3, (((A&7)<<3)|((B>>5)&7)), (B&0x1F)<<3 ]).astype(np.uint8)
Image.fromarray(rgb).resize((640,480), Image.NEAREST).save(OUT + r'\v_rgb565.png')
Image.fromarray(med3(rgb)).resize((640,480), Image.NEAREST).save(OUT + r'\v_rgb565_med.png')

# --- Interpretation 2: YUYV  Y=A, U=B[even], V=B[odd] ---
Y = A.astype(np.float64)
U = np.repeat(B[:, 0::2].astype(np.float64), 2, axis=1)
V = np.repeat(B[:, 1::2].astype(np.float64), 2, axis=1)
u = (U-128)[:,:,None]; v = (V-128)[:,:,None]; y = Y[:,:,None]
yuyv = np.clip(np.dstack([y+1.402*v, y-0.344136*u-0.714136*v, y+1.772*u]), 0, 255).astype(np.uint8)
Image.fromarray(yuyv).resize((640,480), Image.NEAREST).save(OUT + r'\v_yuyv.png')
Image.fromarray(med3(yuyv)).resize((640,480), Image.NEAREST).save(OUT + r'\v_yuyv_med.png')

# --- Interpretation 3: UYVY  Y=B, U=A[even], V=A[odd] ---
Yb = B.astype(np.float64)
Ub = np.repeat(A[:, 0::2].astype(np.float64), 2, axis=1)
Vb = np.repeat(A[:, 1::2].astype(np.float64), 2, axis=1)
ub = (Ub-128)[:,:,None]; vb = (Vb-128)[:,:,None]; yb = Yb[:,:,None]
uyvy = np.clip(np.dstack([yb+1.402*vb, yb-0.344136*ub-0.714136*vb, yb+1.772*ub]), 0, 255).astype(np.uint8)
Image.fromarray(uyvy).resize((640,480), Image.NEAREST).save(OUT + r'\v_uyvy.png')
Image.fromarray(med3(uyvy)).resize((640,480), Image.NEAREST).save(OUT + r'\v_uyvy_med.png')

def struct(img, name):
    g = img.mean(axis=2).astype(np.float64)
    lap = g[1:-1,1:-1]*4 - g[:-2,1:-1] - g[2:,1:-1] - g[1:-1,:-2] - g[1:-1,2:]
    print("  %-14s lapstd %7.2f  adjdiff %6.2f  std %6.2f" % (
        name, lap.std(), np.abs(np.diff(g,axis=1)).mean(), g.std()))

print("\n=== structural score (higher = more image-like) ===")
struct(rgb,  "RGB565")
struct(yuyv, "YUYV")
struct(uyvy, "UYVY")
struct(med3(rgb),  "RGB565+med")
struct(med3(yuyv), "YUYV+med")
struct(med3(uyvy), "UYVY+med")
print("\nsaved v_rgb565.png / v_yuyv.png / v_uyvy.png (+ _med variants)")
