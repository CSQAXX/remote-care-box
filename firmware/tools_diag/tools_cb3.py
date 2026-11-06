import serial, time, re, sys

port = "COM3"

def open_clean(drain_s=2.0):
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
    while not buf.endswith(b"\n"):
        c = s.read(1)
        if not c:
            break
        buf += c
    m = re.search(rb"RAWDUMP (\d+) (\d+)", bytes(buf))
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

s = open_clean(2.0)
print("cbar 1 ->", rawdump(s, "cb_a.bin"))
time.sleep(0.3)
print("cbar 1 ->", rawdump(s, "cb_b.bin"))
time.sleep(0.3)
print("cbar 1 ->", rawdump(s, "cb_c.bin"))
s.close()
