"""找出真正控制画面亮度的寄存器。

已确认:
  - 0x03/0x04 会被传感器 AE 改写 (写不进去) -> 驱动对这两个寄存器的理解是错的
  - 0x0a/0x0c/0x10/0x11/0x12 写进去【能保持住】 -> 可能是真正的曝光控制寄存器

方法: 逐个候选寄存器, 写一个不同���值, 看画面亮度是否跟着变。
亮度变了 -> 它就是亮度控制寄存器, 可以拿来做固定曝光。
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


def cmd(s, c, wait=1.0):
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


def bright(s, n=3):
    v = []
    for i in range(n):
        rawdump(s, f"bgt_{i}.bin")
        a = load(f"bgt_{i}.bin")
        v.append(a.mean() if a is not None else -1)
        time.sleep(0.25)
    return np.median(v)


s = open_clean(3.0)
time.sleep(9.0)

base_regs = parse(cmd(s, "regs 0 0 255", 26.0))
if not base_regs:
    print("!!! 没读到寄存器, 换用 regs 0 255")
    base_regs = parse(cmd(s, "regs 0 255", 26.0))
base_b = bright(s, 5)
print(f"基准亮度 {base_b:.0f} (读到 {len(base_regs)} 个寄存器)\n", flush=True)

cands = [0x0a, 0x0b, 0x0c, 0x0e, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15,
         0x50, 0x56, 0x57, 0x58, 0x59, 0x5d, 0x5e, 0x5f,
         0x94, 0xd0, 0xd1, 0xd2, 0xd3, 0xd5, 0xd6, 0xdb, 0xdc, 0xdf, 0xe0]

for reg in cands:
    orig = base_regs.get(reg)
    if orig is None:
        continue
    # 写一个明显不同的值
    newv = 0x10 if orig != 0x10 else 0x40
    cmd(s, f"wr {reg:02X} {newv:02X}", 0.7)
    time.sleep(1.4)
    after = parse(cmd(s, f"regs 0 0 {reg:02X} {reg:02X}", 1.0)).get(reg)
    b = bright(s, 3)
    stuck = (after == newv)
    changed = abs(b - base_b) / max(base_b, 1) * 100 > 12
    mark = "  <<< 亮度变了!" if changed else ""
    print(f"  0x{reg:02x}: 原 0x{orig:02X} -> 写 0x{newv:02X}, 回读 "
          f"{('--' if after is None else format(after,'02X'))} "
          f"{'保持' if stuck else '被改回'}  亮度 {b:7.0f} (基准 {base_b:.0f}){mark}",
          flush=True)
    cmd(s, f"wr {reg:02X} {orig:02X}", 0.6)
    time.sleep(0.8)
s.close()
