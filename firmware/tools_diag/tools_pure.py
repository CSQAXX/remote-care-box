"""决定性测试: 刚开机, 不做任何传感器操作, 只用 rawdump 连续测亮度。

之前怀疑: 彩条开关(cbar 1/0)会重配传感器, 可能把 AE 弄进震荡状态,
         导致我的测量流程本身制造了"闪烁"假象。
本次测试: 开机后什么都不做, 只测亮度 60 秒。
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


s = open_clean(3.0)
time.sleep(10.0)   # 等固件完全稳定

print("=== 开机后不碰传感器, 连续 60 秒测亮度 ===", flush=True)
t0 = time.time()
br = []
for i in range(60):
    rawdump(s, f"pure_{i}.bin")
    a = load(f"pure_{i}.bin")
    br.append(a.mean() if a is not None else -1)
    t = time.time() - t0
    print(f"  t={t:5.1f}s  亮度 {br[-1]:8.0f}", flush=True)
    time.sleep(0.5)

br = np.array(br)
print(f"\n  min={br.min():.0f} max={br.max():.0f} 幅度={(br.max()-br.min())/br.min()*100:.1f}%")
print(f"  中位={np.median(br):.0f} 标准差={br.std():.1f}")
s.close()
