import serial, time, re, sys

port = "COM3"
DEV = "D:\\Software\\kanhu\\remote-care-box\\firmware"

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
    open(DEV + "\\" + path, "wb").write(data)
    return len(data)

s = open_clean(3.0)
cmd(s, "cbar 0", 1.5)
print("采 3 帧真实场景 (无彩条)", flush=True)
for i in (1, 2, 3):
    n = rawdump(s, f"sc{i}.bin")
    print(f"  sc{i}: {n}", flush=True)
    time.sleep(0.4)
s.close()
