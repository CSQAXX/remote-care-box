import numpy as np
from PIL import Image

OUT = r'D:\Software\kanhu\remote-care-box'


def load(fn):
    return np.frombuffer(open(OUT + '\\' + fn, 'rb').read()[:153600], dtype=np.uint8)


def yuv_to_rgb(b):
    f = b.reshape(240, 320, 2).astype(np.float64)
    Y = f[:, :, 0]
    U = np.repeat(f[:, 0::2, 1], 2, axis=1)
    V = np.repeat(f[:, 1::2, 1], 2, axis=1)
    return np.clip(np.dstack([
        Y + 1.402 * (V - 128),
        Y - 0.344136 * (U - 128) - 0.714136 * (V - 128),
        Y + 1.772 * (U - 128)]), 0, 255).astype(np.uint8)


def stats(rgb, tag):
    def lap(P):
        return float(np.abs(P[1:-1,1:-1]*4 - P[:-2,1:-1] - P[2:,1:-1] - P[1:-1,:-2] - P[1:-1,2:]).mean())
    def corr(P):
        p = P.astype(float); a = p[:, :-1].ravel(); b = p[:, 1:].ravel()
        a -= a.mean(); b -= b.mean()
        return float((a*b).sum() / np.sqrt((a*a).sum()*(b*b).sum()))
    R, G, B = rgb[:,:,0].astype(float), rgb[:,:,1].astype(float), rgb[:,:,2].astype(float)
    print('  %-18s hcorr R%.3f G%.3f B%.3f | lap R%6.2f G%6.2f B%6.2f'
          % (tag, corr(R), corr(G), corr(B), lap(R), lap(G), lap(B)))


print('=== mode3 YUV422 captured over HTTP ===')
for i in range(3):
    b = load('http_truth_%d.bin' % i)
    rgb = yuv_to_rgb(b)
    stats(rgb, 'http_truth_%d' % i)
    Image.fromarray(rgb).resize((640,480), Image.NEAREST).save(OUT + '\\httptruth_%d.png' % i)

print()
print('=== mode3 YUV422 captured over SERIAL ===')
for i in range(3):
    b = load('truth_%d.bin' % i)
    rgb = yuv_to_rgb(b)
    stats(rgb, 'serial_truth_%d' % i)
