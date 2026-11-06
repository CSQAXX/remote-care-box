import numpy as np, urllib.request, time, os
from PIL import Image

W, H = 320, 240
IP = "192.168.2.138"
out = r"D:\Software\kanhu\remote-care-box\firmware\out"
os.makedirs(out, exist_ok=True)

frames = []
for i in range(6):
    d = urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()
    a = np.frombuffer(d, dtype=np.uint8).reshape(H, W, 2)
    frames.append(a)
    time.sleep(0.15)

for i, a in enumerate(frames):
    # 大端 RGB565 -> RGB888
    v = (a[:, :, 0].astype(np.uint32) << 8) | a[:, :, 1].astype(np.uint32)
    r = ((v >> 11) & 0x1F) * 255 // 31
    g = ((v >> 5) & 0x3F) * 255 // 63
    b = (v & 0x1F) * 255 // 31
    img = np.dstack([r, g, b]).astype(np.uint8)
    Image.fromarray(img).save(os.path.join(out, f"f{i}.png"))
    print(f"f{i}: mean={v.mean():.0f}  保存")

# 亮度剖面全分辨率 (每行), 找条带
print()
print("=== 每帧逐行亮度 (每 8 行抽样) ===")
for i, a in enumerate(frames):
    v = (a[:, :, 0].astype(np.uint32) << 8) | a[:, :, 1].astype(np.uint32)
    prof = v.mean(axis=1)
    print(f"  帧{i}: " + " ".join(f"{prof[y]:5.0f}" for y in range(0, H, 8)))
