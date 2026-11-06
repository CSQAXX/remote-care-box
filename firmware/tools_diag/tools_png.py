import numpy as np
from PIL import Image

W, H = 320, 240
data = open("raw_frame.bin", "rb").read()
arr = np.frombuffer(data, dtype=">u2").reshape(H, W)
r = ((arr >> 11) & 0x1F) * 255 // 31
g = ((arr >> 5) & 0x3F) * 255 // 63
b = (arr & 0x1F) * 255 // 31
img = np.dstack([r, g, b]).astype(np.uint8)
Image.fromarray(img).resize((W*2, H*2), Image.NEAREST).save("raw_frame.png")
print("saved", W*2, H*2)
