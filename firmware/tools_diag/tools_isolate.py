"""决定性实验: 亮度明暗交替到底来自"寄存器读干扰"还是"采集缓冲本身"。

A) 只 rawdump, 中间什么都不做           -> 看是否交替
B) rawdump 之间插 regs (I2C 读)          -> 看是否交替
C) rawdump 之间插空等待(不读 I2C)        -> 看是否交替
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


def run(s, tag, between, n=8):
    means = []
    for i in range(n):
        rawdump(s, f"ex_{tag}_{i}.bin")
        a = load(f"ex_{tag}_{i}.bin")
        means.append(a.mean() if a is not None else -1)
        if between == "regs":
            cmd(s, "regs", 3.0)
        elif between == "wait":
            time.sleep(1.2)
    return means


s = open_clean(3.0)
time.sleep(6.0)

for tag, between in (("A", None), ("B", "regs"), ("C", "wait")):
    means = run(s, tag, between)
    alt = "交替!" if all(
        (means[i] < means[i+1]) != (means[i+1] < means[i+2])
        for i in range(len(means) - 2)) else "不交替"
    print(f"[{tag}] between={between or 'none'}")
    print("     " + "  ".join(f"{m:.0f}" for m in means))
    print(f"     -> {alt}\n", flush=True)

s.close()
