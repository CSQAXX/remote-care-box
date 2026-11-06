"""读 AEC 真正的判决寄存器 0xd0-0xd3, 同时看亮度, 判定是不是 AE 在跳。"""
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
time.sleep(6.0)

print("=== 10 轮: 抓帧 + 读 0xd0-0xd3 (AEC 判决) ===", flush=True)
rows = []
for i in range(10):
    rawdump(s, f"aec_{i}.bin")
    r = parse(cmd(s, "regs 0xd0 0xd3", 4.0))
    a = load(f"aec_{i}.bin")
    m = a.mean() if a is not None else -1
    rows.append((r, m))
    print(f"  轮{i}: mean={m:8.0f}  regs={ {hex(k): format(v,'02X') for k,v in sorted(r.items())} }",
          flush=True)
    time.sleep(0.4)

print("\n=== 汇总 ===")
for reg in (0xd0, 0xd1, 0xd2, 0xd3):
    seq = [r.get(reg) for r, _ in rows]
    if any(v is not None for v in seq):
        changed = len({v for v in seq if v is not None}) > 1
        print(f"  0x{reg:02x}: " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in seq)
              + ("   <== 在变" if changed else ""))

ms = [m for _, m in rows]
print(f"\n  亮度: " + " ".join(f"{m:.0f}" for m in ms))
print(f"  幅度 {100*(max(ms)-min(ms))/min(ms):.0f}%")
s.close()
