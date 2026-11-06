"""最终验收: 三个根因的修复是否都生效, 且互相不冲突。

1) 花屏: PSRAM 直写关闭 + PCLK 四分频
   -> 彩条帧间 bit 翻转率
2) 闪烁: AEC 锁定
   -> 连续帧亮度幅度 + 0xd4 稳定性
3) LCD:  SPI DMA 双缓冲 + ISR 安全信号量
   -> 无掉帧, 帧率稳定
"""
import serial, time, re, socket, numpy as np

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


def parse(lines):
    out = {}
    for l in lines:
        m = re.search(r"REG 0x([0-9a-fA-F]{2}) = 0x([0-9a-fA-F]{2})", l)
        if m:
            out[int(m.group(1), 16)] = int(m.group(2), 16)
    return out


def bitflip(a, b):
    x = a ^ b
    return 100 * sum(((x >> bit) & 1).sum() for bit in range(16)) / 16 / (W * H)


print("=" * 70)
print("  ATK-DNESP32S3-BOX3  最终验收 (三个根因一起验)")
print("=" * 70)

s = open_clean(3.0)
time.sleep(7.0)

print("\n[1] 生效配置 (cfg)")
for l in cmd(s, "cfg", 4.0):
    t = re.sub(r"\x1b\[[0-9;]*m", "", l).strip()
    if any(k in t for k in ("采集配置", "PSRAM", "PCLK", "传感器模式", "自动曝光", "=====")):
        print("    ", t[-88:])

print("\n[2] 花屏指标: 彩条(静止图样) 帧间 bit 翻转率")
cmd(s, "cbar 1", 1.5)
time.sleep(3.5)
cbs = []
for i in range(4):
    rawdump(s, f"fa_cb{i}.bin")
    cbs.append(load(f"fa_cb{i}.bin"))
    time.sleep(0.4)
cmd(s, "cbar 0", 1.0)
flips = [bitflip(cbs[i], cbs[i+1]) for i in range(3)]
for i, f in enumerate(flips):
    print(f"    帧{i} vs 帧{i+1}: bit翻转率 {f:6.2f}%   {'OK' if f < 5 else 'FAIL'}")
worst_flip = max(flips)

print("\n[3] 闪烁指标: 连续帧亮度稳定性 + AEC 判决值")
time.sleep(2.5)
br, d4v = [], []
for i in range(10):
    rawdump(s, f"fa_sc{i}.bin")
    a = load(f"fa_sc{i}.bin")
    br.append(a.mean() if a is not None else -1)
    r = parse(cmd(s, "regs 0xd4 0xd4", 3.0))
    d4v.append(r.get(0xd4))
    time.sleep(0.3)
br = np.array(br)
rng = (br.max() - br.min()) / br.min() * 100
print("    亮度: " + " ".join(f"{v:.0f}" for v in br))
print("    0xd4: " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in d4v))
print(f"    亮度幅度 {rng:.1f}%   0xd4 变化数 {len(set(d4v))}   "
      f"{'OK' if rng < 10 else 'FAIL'}")

print("\n[4] 帧率 / 掉帧")
fps = None
t0 = time.time()
s.write(b"\n"); s.flush()
while time.time() - t0 < 8:
    try:
        line = s.readline()
        if line and b"PROD" in line:
            txt = re.sub(r"\x1b\[[0-9;]*m", "", line.decode("utf-8", "replace"))
            print("   ", txt[-92:])
            m = re.search(r"\(([\d.]+) fps\)", txt)
            if m:
                fps = float(m.group(1))
            break
    except Exception:
        pass
for l in cmd(s, "stat", 2.0):
    if "cam_frames" in l:
        print("   ", re.sub(r"\x1b\[[0-9;]*m", "", l)[-80:])

print("\n[5] MJPEG 流")
try:
    sk = socket.create_connection(("192.168.2.138", 8080), timeout=10)
    sk.sendall(b"GET /stream HTTP/1.1\r\nHost: 192.168.2.138\r\n\r\n")
    buf = bytearray()
    t0 = time.time()
    while len(buf) < 400000 and time.time() - t0 < 20:
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
    print(f"    {len(buf)} 字节, {n} 帧 JPEG   {'OK' if n > 3 else 'FAIL'}")
except Exception as e:
    print("    FAIL:", e)
s.close()

print("\n" + "=" * 70)
print(f"  结论: bit翻转 {worst_flip:.2f}% (<5 OK) | 亮度幅度 {rng:.1f}% (<10 OK) | "
      f"帧率 {fps} fps")
print("=" * 70)
