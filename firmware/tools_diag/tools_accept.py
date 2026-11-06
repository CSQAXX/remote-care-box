"""最终验收脚本: 验证开机默认配置 + 采集链路逐位一致 + MJPEG 流正常。"""
import serial, time, re, socket, sys
import numpy as np

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


def cmd(s, c, wait=2.0):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                out.append(line.decode("utf-8", "replace").rstrip())
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
    p = DEV + "\\" + path
    open(p, "wb").write(data)
    return len(data)


def load(p):
    d = open(DEV + "\\" + p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)


def bitflip(a, b):
    x = a ^ b
    return 100 * sum(((x >> bit) & 1).sum() for bit in range(16)) / 16 / (W * H)


print("=" * 68)
print("ATK-DNESP32S3-BOX3  花屏问题 —— 最终验收")
print("=" * 68)

s = open_clean(3.0)

# ---- 1. 开机默认配置 ----
print("\n[1] 开机默认配置 (需要重启抓日志, 这里用运行时查询交叉验证)")
for l in cmd(s, "q", 3.0):
    if "QUAL" in l:
        print("    ", l[-100:])

# ---- 2. 彩条 (静止图样) 帧间 bit 翻转率 ----
print("\n[2] 彩条静止帧间 bit 翻转率 (纯链路噪声指标)")
cmd(s, "cbar 1", 1.5)
time.sleep(3.0)
cbs = []
for i in range(3):
    rawdump(s, f"acc_cb{i}.bin")
    cbs.append(load(f"acc_cb{i}.bin"))
    time.sleep(0.4)
cmd(s, "cbar 0", 1.0)
for i in range(2):
    fl = bitflip(cbs[i], cbs[i+1])
    print(f"    帧{i} vs 帧{i+1}: bit翻转率 {fl:5.2f}%  {'OK' if fl < 5 else 'FAIL'}")

# ---- 3. 真实场景帧间一致性 (归一化自动曝光) ----
print("\n[3] 真实场景帧间一致性 (归一化掉自动曝光)")
time.sleep(1.5)
scs = []
for i in range(4):
    rawdump(s, f"acc_sc{i}.bin")
    scs.append(load(f"acc_sc{i}.bin"))
    time.sleep(0.35)
worst = 0.0
for i in range(3):
    a = scs[i].astype(np.float64); b = scs[i+1].astype(np.float64)
    na = a / (a.mean() or 1); nb = b / (b.mean() or 1)
    d = np.abs(na - nb)
    pct = 100 * (d > 4).sum() / (W * H)
    worst = max(worst, pct)
    print(f"    帧{i} vs 帧{i+1}: 差异>4 {pct:5.2f}%  max|Δ| {d.max():6.1f}  "
          f"{'OK' if pct == 0 else 'FAIL'}")
print(f"    >>> 最差配对: {worst:.2f}%")

# ---- 4. 空间噪声 (3x3 中值残差) ----
print("\n[4] 空间噪声 (3x3 中值滤波残差)")
if scs[0] is not None:
    a = scs[0]
    for nm, mask, sh in (("R5", 0xF800, 11), ("G6", 0x07E0, 5), ("B5", 0x001F, 0)):
        ch = ((a & mask) >> sh).astype(np.float64)
        p = np.pad(ch, 1, mode="edge")
        st = np.stack([p[0:-2,0:-2],p[0:-2,1:-1],p[0:-2,2:],
                       p[1:-1,0:-2],p[1:-1,1:-1],p[1:-1,2:],
                       p[2:,0:-2],p[2:,1:-1],p[2:,2:]], axis=0)
        r = np.abs(ch - np.median(st, axis=0))
        print(f"    {nm}: 残差均值 {r.mean():.3f}  >3 占比 {100*(r>3).sum()/ch.size:.2f}%")

# ---- 5. 帧率 ----
print("\n[5] 帧率")
fps = None
for l in cmd(s, "q", 1.0):
    pass
time.sleep(3)
t0 = time.time()
s.write(b"\n")
s.flush()
while time.time() - t0 < 8:
    try:
        line = s.readline()
        if line and b"PROD" in line:
            txt = line.decode("utf-8", "replace")
            print("   ", txt[-90:])
            m = re.search(r"\(([\d.]+) fps\)", txt)
            if m:
                fps = float(m.group(1))
            break
    except Exception:
        pass
s.close()

# ---- 6. MJPEG 流 ----
print("\n[6] 浏览器 MJPEG 流")
try:
    sk = socket.create_connection(("192.168.2.138", 8080), timeout=10)
    sk.sendall(b"GET /stream HTTP/1.1\r\nHost: 192.168.2.138\r\n\r\n")
    buf = bytearray()
    t0 = time.time()
    while len(buf) < 300000 and time.time() - t0 < 15:
        try:
            ch = sk.recv(65536)
            if not ch:
                break
            buf += ch
        except socket.timeout:
            break
    sk.close()
    body = bytes(buf).split(b"\r\n\r\n", 1)[-1]
    n = body.count(b"\xff\xd8\xff")
    print(f"    收到 {len(buf)} 字节, JPEG 帧数 {n}  {'OK' if n > 3 else 'FAIL'}")
except Exception as e:
    print("    FAIL:", e)

print("\n" + "=" * 68)
print("结论: 采集链路 bit 翻转率 " +
      f"{bitflip(cbs[0], cbs[1]):.2f}% (原 49.15%), "
      f"场景帧间差异 {worst:.2f}% (原 100%), 帧率 {fps} fps")
print("=" * 68)
