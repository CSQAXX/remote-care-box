import numpy as np
from PIL import Image

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

def to_img(arr):
    r = ((arr >> 11) & 0x1F) * 255 // 31
    g = ((arr >> 5) & 0x3F) * 255 // 63
    bl = (arr & 0x1F) * 255 // 31
    return np.dstack([r, g, bl]).astype(np.uint8)

# 用刚才 tools_noise_vs_lum 抓的两帧 (亮/暗)
import urllib.request, time
frames = []
for i in range(6):
    d = urllib.request.urlopen("http://192.168.2.138:8080/raw?capture=1", timeout=30).read()
    frames.append(np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32))
    time.sleep(0.3)

for i, f in enumerate(frames):
    Image.fromarray(to_img(f)).resize((W*2, H*2), Image.NEAREST).save(f"n{i}.png")
    print(f"n{i}.png  亮度={f.mean():.0f}")
print("saved")
