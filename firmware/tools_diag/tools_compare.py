"""生成对比图: 亮帧 / 暗帧 / 各自拉伸后的对比 / 差值图"""
import numpy as np, urllib.request, time, os
from PIL import Image

W, H = 320, 240
IP = "192.168.2.138"
out = r"D:\Software\kanhu\remote-care-box\firmware\out"
os.makedirs(out, exist_ok=True)


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.float64)


def to_rgb(a):
    v = np.clip(a, 0, 65535).astype(np.uint32)
    r = ((v >> 11) & 0x1F) * 255 // 31
    g = ((v >> 5) & 0x3F) * 255 // 63
    b = (v & 0x1F) * 255 // 31
    return np.dstack([r, g, b]).astype(np.uint8)


def stretch(a):
    """每通道独立拉伸到 0..255, 看清结构"""
    out = np.zeros_like(a)
    for c in range(3):
        lo, hi = a[:, :, c].min(), a[:, :, c].max()
        out[:, :, c] = ((a[:, :, c] - lo) * 255 / max(hi - lo, 1)).astype(np.uint8)
    return out


frames = []
for i in range(8):
    frames.append(load(raw()))
    time.sleep(0.2)

lum = np.array([f.mean() for f in frames])
bi, di = int(np.argmax(lum)), int(np.argmin(lum))
print(f"亮帧#{bi} luma={lum[bi]:.0f}   暗帧#{di} luma={lum[di]:.0f}")

b, d = frames[bi], frames[di]
# 暗帧按最佳增益放大
k = float((b * d).sum() / (d * d).sum())
print(f"最佳增益 k={k:.3f}")

rgb_b, rgb_d = to_rgb(b), to_rgb(d)
st_b, st_d = stretch(rgb_b), stretch(rgb_d)

# 差值图 (归一化)
diff = np.abs(b - d * k)
dv = to_rgb(diff * 8)

rows = [
    ("亮帧", rgb_b), ("暗帧", rgb_d),
    ("亮帧-拉伸", st_b), ("暗帧-拉伸", st_d),
    ("|亮-暗*k| x8", dv),
]
imgs = [r[1] for r in rows]
canvas = np.vstack([np.hstack(imgs[0:2]), np.hstack(imgs[2:4]),
                    np.hstack([imgs[4], np.zeros_like(imgs[4])])])
Image.fromarray(canvas).save(os.path.join(out, "compare.png"))
print("已保存 compare.png (行: 亮帧|暗帧 / 亮帧拉伸|暗帧拉伸 / 差值图)")

# 每行独立拟合 k, 看 k 随行号怎么变
rowk = np.array([(b[y] * d[y]).sum() / max((d[y] * d[y]).sum(), 1) for y in range(H)])
print()
print("逐行最佳增益 k (每 8 行):")
for y in range(0, H, 8):
    print(f"  行{y:3d}: k={rowk[y]:.3f}  亮度 亮={b[y].mean():7.0f} 暗={d[y].mean():7.0f} 比={b[y].mean()/max(d[y].mean(),1):.3f}")
