"""
决定性判别: 摄像头数据到底有没有问题?

两个互相独立的消费者读【同一个 fb->buf】:
  A) /raw   -> 原始 RGB565
  B) /stream -> JPEG (经 esp_jpeg 编码)
如果两者内容一致 -> 摄像头数据没问题 -> 问题在 LCD 显示路径。
如果两者本身就花 -> 摄像头数据有问题。

同时分析"亮帧/暗帧"到底是同一场景的不同曝光, 还是内容真的变了。
"""
import numpy as np
import urllib.request, time, os
from PIL import Image
import io

W, H = 320, 240
IP = "192.168.2.138"
os.chdir(r"D:\Software\kanhu\remote-care-box\firmware")


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def jpeg():
    # 从 /stream 取一帧 JPEG (multipart)
    r = urllib.request.urlopen(f"http://{IP}:8080/stream", timeout=30)
    buf = b""
    for _ in range(200):
        c = r.read(1)
        if not c:
            break
        buf += c
        if buf.endswith(b"\r\n"):
            break
    # 之后是 JPEG 数据, 读到 FFD9
    data = bytearray()
    while True:
        c = r.read(1)
        if not c:
            break
        data += c
        if len(data) > 3 and data[-2:] == b"\xff\xd9":
            break
    return bytes(data)


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int64)


def to_img(a):
    r = ((a >> 11) & 0x1F) * 255 // 31
    g = ((a >> 5) & 0x3F) * 255 // 63
    b = (a & 0x1F) * 255 // 31
    return np.dstack([r, g, b]).astype(np.uint8)


print("=== 抓 6 组 (原始 + JPEG) ===")
for i in range(6):
    d = raw()
    a = load(d)
    try:
        j = jpeg()
        ok = len(j) > 100 and j[:2] == b"\xff\xd8"
        jinfo = f"JPEG {len(j)}B {'ok' if ok else 'BAD'}"
    except Exception as e:
        jinfo = f"JPEG 失败 {e}"
    print(f"  帧{i}: 原始 亮度={a.mean():8.0f} hf_p90={np.percentile(np.abs(np.diff(a.astype(float),axis=1)),90):7.1f}  {jinfo}")
    Image.fromarray(to_img(a)).resize((W*2, H*2), Image.NEAREST).save(f"cmp{i}.png")
    time.sleep(0.3)

print()
print("=== 亮帧 vs 暗帧: 是同一场景的不同曝光, 还是内容不同? ===")
frames = []
for i in range(10):
    frames.append(load(raw()))
    time.sleep(0.25)
lums = [f.mean() for f in frames]
bi = int(np.argmax(lums))
di = int(np.argmin(lums))
B, D = frames[bi].astype(float), frames[di].astype(float)
print(f"  最亮帧#{bi} 亮度{B.mean():.0f}   最暗帧#{di} 亮度{D.mean():.0f}")
print(f"  Pearson 相关 (原始): {np.corrcoef(B.ravel(), D.ravel())[0,1]:.4f}")
# 缩放到同亮度再比
k = B.mean() / D.mean()
Ds = D * k
print(f"  缩放 k={k:.3f} 后相关: {np.corrcoef(B.ravel(), Ds.ravel())[0,1]:.4f}")
print(f"  缩放后平均绝对误差: {np.abs(B-Ds).mean():.1f} (满量程 65535)")
print(f"  缩放后 >10% 的像素占比: {100*(np.abs(B-Ds) > 0.1*np.maximum(B,1)).mean():.2f}%")
