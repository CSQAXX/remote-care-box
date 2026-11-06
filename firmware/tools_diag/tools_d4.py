"""决定性验证: 关掉 AEC 之后, 0xd4 还变不变? 亮度还跳不跳?

如果 0xd4 不再变化但亮度还在跳 -> 说明还有别的因素。
如果 0xd4 和亮度都不再变化 -> AE 就是唯一原因, 把它固化成默认即可。
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


def cmd(s, c, wait=2.0, show=False):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                t = line.decode("utf-8", "replace").rstrip()
                out.append(t)
                if show:
                    print("    ", t[-85:])
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


def series(s, tag, n=10):
    rows = []
    for i in range(n):
        rawdump(s, f"cd2_{tag}_{i}.bin")
        a = load(f"cd2_{tag}_{i}.bin")
        m = a.mean() if a is not None else -1
        r = parse(cmd(s, "regs 0xd0 0xd4", 4.0))
        r22 = parse(cmd(s, "regs 0x22 0x22", 3.0))
        rows.append((m, r.get(0xd4), r22.get(0x22)))
        time.sleep(0.3)
    br = np.array([x[0] for x in rows])
    d4 = [x[1] for x in rows]
    r22 = [x[2] for x in rows]
    rng = (br.max()-br.min())/max(br.min(),1e-9)*100
    print(f"  [{tag}] 亮度: " + " ".join(f"{v:.0f}" for v in br))
    print(f"         0xd4: " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in d4)
          + f"   0x22: " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in r22))
    print(f"         亮度幅度 {rng:.0f}%   0xd4 变化数 {len(set(d4))}", flush=True)
    return rng, len(set(d4))


s = open_clean(3.0)
time.sleep(6.0)

print("=== 基线: AE 自动 ===")
series(s, "auto")

print("\n=== aelock 1 (关 AEC/AGC, 保留 AWB) ===")
cmd(s, "aelock 1", 3.0, show=True)
time.sleep(3.0)
series(s, "lock")

print("\n=== 手动曝光 expo 480 gain 20 ===")
cmd(s, "expo 480 20", 2.0, show=True)
time.sleep(3.0)
series(s, "manual")

print("\n=== 恢复 AE 自动 ===")
cmd(s, "aelock 0", 2.0)
time.sleep(2.0)
series(s, "unlock")
s.close()
