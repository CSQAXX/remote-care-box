import serial, time, re, sys

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

def send(s, cmd, wait=2.0):
    s.write((cmd + "\n").encode())
    s.flush()
    t0 = time.time()
    lines = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                lines.append(line.decode("utf-8", "replace").rstrip())
        except Exception:
            pass
    return lines

def rawdump(s, path):
    s.write(b"rawdump\n")
    s.flush()
    buf = bytearray()
    needle = b"RAWDUMP "
    while not buf.endswith(needle):
        c = s.read(1)
        if not c:
            return None, "no header"
        buf += c
    hdr = bytes(buf)
    while not hdr.endswith(b"\n"):
        c = s.read(1)
        if not c:
            break
        hdr += c
    m = re.search(rb"RAWDUMP (\d+) (\d+)", hdr)
    if not m:
        return None, hdr[:80]
    n = int(m.group(2))
    data = bytearray()
    while len(data) < n:
        chunk = s.read(min(65536, n - len(data)))
        if not chunk:
            break
        data += chunk
    open(path, "wb").write(data)
    # uptime timestamp from header
    ts = re.search(rb"I \((\d+)\)", hdr)
    return len(data), (ts.group(1).decode() if ts else "?")

s = open_clean(3.0)
print("== cbar 1 ==")
for l in send(s, "cbar 1", 2.0):
    if "kanhu" in l: print("  ", l[-80:])
time.sleep(2.0)
for name in ("cb_a.bin", "cb_b.bin"):
    n, ts = rawdump(s, name)
    print(f"  {name}: {n} bytes, board_uptime_at_capture={ts}ms")
    time.sleep(0.5)
print("== cbar 0 ==")
for l in send(s, "cbar 0", 1.5):
    if "kanhu" in l: print("  ", l[-80:])
s.close()
