"""决定性判别: 彩条(传感器内部生成, 亮度固定)的亮度会不会也跳?

- 彩条亮度也跳  -> 问题在传感器数字通路/帧缓冲路径 (AE 无从下手)
- 彩条亮度稳定  -> 确实是 AE 在根据场景来回调

同时记录 0xd4 看它和彩条亮度的关系。
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


def parse(lines):
    out = {}
    for l in lines:
        m = re.search(r"REG 0x([0-9a-fA-F]{2}) = 0x([0-9a-fA-F]{2})", l)
        if m:
            out[int(m.group(1), 16)] = int(m.group(2), 16)
    return out


def run(s, tag, n=14, gap=0.5):
    br, d4 = [], []
    for i in range(n):
        rawdump(s, f"cbt_{tag}_{i}.bin")
        a = load(f"cbt_{tag}_{i}.bin")
        br.append(a.mean() if a is not None else -1)
        d4.append(parse(cmd(s, "regs 0xd4 0xd4", 1.0)).get(0xd4))
        time.sleep(gap)
    br = np.array(br)
    rng = (br.max() - br.min()) / br.min() * 100
    print(f"  [{tag}] 亮度: " + " ".join(f"{v:.0f}" for v in br) + f"   幅度 {rng:.0f}%")
    print(f"         0xd4: " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in d4),
          flush=True)
    return rng


s = open_clean(3.0)
time.sleep(7.0)

print("=== A) 真实场景 ===", flush=True)
run(s, "scene")

print("\n=== B) 传感器内置彩条 (亮度固定, 不经过镜头) ===", flush=True)
cmd(s, "cbar 1", 2.0)
time.sleep(3.0)
run(s, "cbar")
cmd(s, "cbar 0", 1.0)
time.sleep(2.0)

print("\n=== C) 真实场景 (彩条关掉后) ===", flush=True)
run(s, "scene2")
s.close()
