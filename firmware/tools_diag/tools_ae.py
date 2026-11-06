"""判定"画面亮度跳动"是传感器 AE 行为还是数据链路问题。

方法:
  连续 6 轮, 每轮同时抓:
    - 一帧原始 RGB565 (rawdump)
    - GC0308 page0 曝光/增益相关寄存器 (regs)
  然后看: 寄存器在变 -> AE; 寄存器不变而亮度在跳 -> 链路问题。
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


def parse_regs(lines):
    out = {}
    for l in lines:
        m = re.search(r"REG 0x([0-9a-f]{2}) = 0x([0-9a-f]{2})", l)
        if m:
            out[int(m.group(1), 16)] = int(m.group(2), 16)
    return out


s = open_clean(3.0)
time.sleep(6.0)

samples = []
for i in range(6):
    rawdump(s, f"ae_{i}.bin")
    regs = parse_regs(cmd(s, "regs", 4.0))
    a = load(f"ae_{i}.bin")
    samples.append((regs, a))
    if a is not None:
        print(f"  轮{i}: mean={a.mean():8.0f}  regs读完 {len(regs)} 个", flush=True)
    else:
        print(f"  轮{i}: rawdump 失败", flush=True)
    time.sleep(0.6)

print("\n=== 亮度序列 ===")
means = [a.mean() if a is not None else 0 for _, a in samples]
for i, m in enumerate(means):
    print(f"  轮{i}: {m:.0f}")

print("\n=== 曝光/增益相关寄存器序列 ===")
watch = [0x00, 0x01, 0x03, 0x04, 0x10, 0x11, 0x13, 0x14, 0x15, 0x56]
for r in watch:
    seq = [regs.get(r) for regs, _ in samples]
    if any(v is not None for v in seq):
        changed = any(seq[i] != seq[0] for i in range(len(seq)) if seq[i] is not None)
        mark = "  <== 在变" if changed else ""
        print(f"  0x{r:02x}: " + " ".join(f"{('--' if v is None else format(v,'02X')):>3s}" for v in seq) + mark)

print("\n=== 结论 ===")
all_regs = [regs for regs, _ in samples if regs]
if len(all_regs) >= 2:
    diffs = {}
    for r in set(all_regs[0].keys()) & set(all_regs[-1].keys()):
        if all_regs[0][r] != all_regs[-1][r]:
            diffs[r] = (all_regs[0][r], all_regs[-1][r])
    if diffs:
        print("  有寄存器在变 -> 传感器 AE/AGC 在工作:")
        for r, (a, b) in sorted(diffs.items()):
            print(f"    0x{r:02x}: {a:02X} -> {b:02X}")
    else:
        print("  所有寄存器都不变")
print(f"  亮度波动: min={min(means):.0f} max={max(means):.0f} "
      f"幅度={100*(max(means)-min(means))/min(means):.1f}%")
s.close()
