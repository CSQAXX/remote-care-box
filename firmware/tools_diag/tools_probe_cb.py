"""排查: 彩条开关切换后传感器是否完全恢复, 以及空间噪声的真实值。"""
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
    while time.time() - t0 < wait:
        try:
            s.readline()
        except Exception:
            pass


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


def resid(a, mask, sh):
    ch = ((a & mask) >> sh).astype(np.float64)
    p = np.pad(ch, 1, mode="edge")
    st = np.stack([p[0:-2,0:-2],p[0:-2,1:-1],p[0:-2,2:],
                   p[1:-1,0:-2],p[1:-1,1:-1],p[1:-1,2:],
                   p[2:,0:-2],p[2:,1:-1],p[2:,2:]], axis=0)
    r = np.abs(ch - np.median(st, axis=0))
    return r.mean(), 100*(r > 3).sum()/ch.size


s = open_clean(3.0)
cmd(s, "cfg", 2.0)

print("=== A) 纯场景基线 (没动过彩条) ===", flush=True)
time.sleep(2.0)
fs = []
for i in range(4):
    rawdump(s, f"pb_{i}.bin")
    fs.append(load(f"pb_{i}.bin"))
    time.sleep(0.4)
for i in range(3):
    print(f"  帧{i} vs 帧{i+1}: bit翻转 {bitflip(fs[i],fs[i+1]):5.2f}%")
for nm, m, sh in (("R5",0xF800,11),("G6",0x07E0,5),("B5",0x001F,0)):
    r, p = resid(fs[0], m, sh)
    print(f"  {nm} 空间残差: 均值 {r:.3f}  >3 占比 {p:.2f}%")

print("\n=== B) 开彩条 -> 关彩条, 逐帧看恢复过程 ===", flush=True)
cmd(s, "cbar 1", 1.5)
time.sleep(3.0)
cb = []
for i in range(3):
    rawdump(s, f"pc_cb{i}.bin")
    cb.append(load(f"pc_cb{i}.bin"))
    time.sleep(0.4)
for i in range(2):
    print(f"  彩条 帧{i} vs 帧{i+1}: bit翻转 {bitflip(cb[i],cb[i+1]):5.2f}%")

cmd(s, "cbar 0", 1.0)
rec = []
for i in range(8):
    time.sleep(0.8)
    rawdump(s, f"pc_rec{i}.bin")
    rec.append(load(f"pc_rec{i}.bin"))
    print(f"  恢复后第{i}帧: 与上一彩条帧 bit翻转 {bitflip(cb[-1],rec[-1]):5.2f}%  "
          f"mean={rec[-1].mean():.0f}", flush=True)

print("\n  恢复后帧之间 bit翻转:")
for i in range(len(rec)-1):
    print(f"    rec{i} vs rec{i+1}: {bitflip(rec[i],rec[i+1]):5.2f}%")
s.close()
