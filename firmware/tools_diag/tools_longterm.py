"""长时间连续观测: 亮度 + 0xd4 的真实变化规律。

目的: 判断 0xd4 的变化是
  (a) 周期性震荡 (AE hunting, 有固定周期), 还是
  (b) 随机跳变。
并测出周期, 便于判断我的采样窗口是不是刚好落在稳定段。

采样间隔 0.4s, 120 个样本 = 48 秒。
"""
import serial, time, re, numpy as np

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


s = open_clean(3.0)
time.sleep(7.0)
cmd(s, "aelock 1", 3.0)
time.sleep(2.0)

print("=== 60 秒连续观测 (aelock 1 后) ===", flush=True)
t_start = time.time()
prev = None
for i in range(120):
    rawdump(s, f"lt_{i}.bin")
    a = load(f"lt_{i}.bin")
    br = a.mean() if a is not None else -1
    d4 = parse(cmd(s, "regs 0xd4 0xd4", 2.5)).get(0xd4)
    t = time.time() - t_start
    flag = ""
    if prev is not None and d4 is not None and prev[1] is not None and d4 != prev[1]:
        flag = f"  <== 0xd4 {prev[1]:02X} -> {d4:02X}  (t={t:.1f}s)"
    print(f"  t={t:6.1f}s  亮度 {br:8.0f}  0xd4={('--' if d4 is None else format(d4,'02X'))}{flag}",
          flush=True)
    prev = (br, d4)
s.close()
