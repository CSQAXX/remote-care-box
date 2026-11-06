"""最终验收 v2: 三个根因一起验, 用【稳定之后的输出】做判断。

1) 花屏: 彩条帧间 bit 翻转率 (rawdump, 稳定前, 但花屏与亮度无关)
2) 闪烁: MJPEG 流里的连续帧亮度稳定性 (稳定后)
3) 稳定性: 帧率 / 掉帧 / JPEG 流
"""
import serial, time, re, socket, numpy as np
from PIL import Image
import io

PORT = "COM3"
DEV = r"D:\Software\kanhu\remote-care-box\firmware"
W, H = 320, 240


def open_clean(drain_s=3.0):
    s = serial.Serial(PORT, 115200, timeout=0.2)
    try:
        s.dtr = False
        s.rts = False
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < drain_s:
        try:
            s.readline()
        except Exception:
            pass
    return s


def cmd(s, c, wait=1.5):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                out.append(line.decode("utf-8", "replace"))
        except Exception:
            pass
    return out


def rawdump(s, path):
    s.write(b"rawdump\n")
    s.flush()
    buf = bytearray()
    while not buf.endswith(b"RAWDUMP "):
        c = s.read(1)
        if not c:
            return None
        buf += c
    hdr = bytes(buf)
    while not hdr.endswith(b"\n"):
        c = s.read(1)
        if not c:
            break
        hdr += c
    m = re.search(rb"RAWDUMP (\d+) (\d+)", hdr)
    if not m:
        return None
    n = int(m.group(2))
    data = bytearray()
    while len(data) < n:
        chunk = s.read(min(65536, n - len(data)))
        if not chunk:
            break
        data += chunk
    open(DEV + "\\" + path, "wb").write(data)
    return len(data)


def load(p):
    d = open(DEV + "\\" + p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)


def bitflip(a, b):
    x = a ^ b
    return 100 * sum(((x >> bit) & 1).sum() for bit in range(16)) / 16 / (W * H)


def stream_frames(seconds=30):
    sk = socket.create_connection(("192.168.2.138", 8080), timeout=10)
    sk.sendall(b"GET /stream HTTP/1.1\r\nHost: 192.168.2.138\r\n\r\n")
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            c = sk.recv(65536)
            if not c:
                break
            buf += c
        except socket.timeout:
            break
    sk.close()
    body = bytes(buf).split(b"\r\n\r\n", 1)[-1]
    frames = []
    i = 0
    while True:
        a = body.find(b"\xff\xd8\xff", i)
        if a < 0:
            break
        b = body.find(b"\xff\xd9", a)
        if b < 0:
            break
        frames.append(body[a:b+2])
        i = b + 2
    return frames, len(buf)


print("=" * 72)
print("   ATK-DNESP32S3-BOX3  最终验收 v2")
print("=" * 72)

s = open_clean(3.0)
time.sleep(8.0)

print("\n[1] 生效配置")
for l in cmd(s, "cfg", 4.0):
    t = re.sub(r"\x1b\[[0-9;]*m", "", l).strip()
    if any(k in t for k in ("采集配置", "PSRAM", "PCLK", "传感器模式", "自动曝光")):
        print("    ", t[-86:])
for l in cmd(s, "bstab", 2.0):
    t = re.sub(r"\x1b\[[0-9;]*m", "", l).strip()
    if "亮度稳定器" in t:
        print("    ", t[-86:])

print("\n[2] 花屏指标: 彩条(静止图样) 帧间 bit 翻转率")
cmd(s, "cbar 1", 1.5)
time.sleep(3.0)
cbs = []
for i in range(4):
    rawdump(s, f"v2_cb{i}.bin")
    cbs.append(load(f"v2_cb{i}.bin"))
    time.sleep(0.4)
cmd(s, "cbar 0", 1.0)
flips = []
for i in range(3):
    if cbs[i] is not None and cbs[i+1] is not None:
        f = bitflip(cbs[i], cbs[i+1])
        flips.append(f)
        print(f"    帧{i} vs 帧{i+1}: {f:6.2f}%   {'OK' if f < 6 else 'FAIL'}")
worst = max(flips) if flips else 99

print("\n[3] 闪烁指标: MJPEG 流里的连续帧亮度 (稳定后的输出)")
frames, nbytes = stream_frames(30)
lums = []
for f in frames:
    im = Image.open(io.BytesIO(f)).convert("L")
    lums.append(float(np.asarray(im).mean()))
a = np.array(lums)
rng = (a.max() - a.min()) / a.min() * 100
step = max(1, len(a)//12)
for k in range(0, len(a), step):
    print("     " + "  ".join(f"{v:6.1f}" for v in a[k:k+step]))
print(f"    min={a.min():.1f} max={a.max():.1f} 标准差={a.std():.2f} 幅度={rng:.1f}%  "
      f"{'OK (稳定)' if rng < 12 else 'FAIL'}")

print("\n[4] 帧率 / 掉帧 / JPEG")
fps = None
t0 = time.time()
while time.time() - t0 < 8:
    try:
        line = s.readline()
        if line and b"PROD" in line:
            txt = re.sub(r"\x1b\[[0-9;]*m", "", line.decode("utf-8", "replace"))
            print("   ", txt[-90:])
            m = re.search(r"\(([\d.]+) fps\)", txt)
            if m:
                fps = float(m.group(1))
            break
    except Exception:
        pass
for l in cmd(s, "stat", 2.0):
    if "cam_frames" in l:
        print("   ", re.sub(r"\x1b\[[0-9;]*m", "", l)[-80:])
print(f"    MJPEG: {nbytes} 字节, {len(frames)} 帧 JPEG   "
      f"{'OK' if len(frames) > 10 else 'FAIL'}")

s.close()
print("\n" + "=" * 72)
print(f"  结论: 花屏 bit翻转 {worst:.2f}% (<6 OK) | 亮度幅度 {rng:.1f}% (<12 OK) | "
      f"帧率 {fps} fps | JPEG {len(frames)} 帧")
print("=" * 72)
