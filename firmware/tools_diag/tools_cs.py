import serial, time, re

port = "COM3"

def open_clean(drain_s=2.5):
    s = serial.Serial(port, 115200, timeout=0.2)
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
    needle = b"RAWDUMP "
    while not buf.endswith(needle):
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
    open(path, "wb").write(data)
    return len(data)

s = open_clean(3.0)
s.write(b"cbar 1\n")
s.flush()
time.sleep(4.0)          # 等彩条生效 + 摄像头稳定
for i, name in enumerate(("cs1.bin", "cs2.bin", "cs3.bin")):
    n = rawdump(s, name)
    print(f"{name}: {n}")
    time.sleep(0.6)
s.write(b"cbar 0\n")
s.flush()
s.close()
