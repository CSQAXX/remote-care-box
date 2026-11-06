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

def trial(s, tag):
    """开彩条, 抓两帧, 算帧间差异"""
    cmd(s, "cbar 1", 1.5)
    time.sleep(3.0)
    rawdump(s, f"t_{tag}_a.bin")
    time.sleep(0.6)
    rawdump(s, f"t_{tag}_b.bin")
    cmd(s, "cbar 0", 1.0)
    print(f"  [{tag}] done", flush=True)

s = open_clean(3.0)
print("baseline (mode0 + pda1 + pclk1)", flush=True)
trial(s, "base")

cmd(s, "pda 0", 6.0)
print("pda 0", flush=True)
trial(s, "pda0")

cmd(s, "pclk 2", 2.0)
print("pclk 2", flush=True)
trial(s, "pclk2")

cmd(s, "pclk 3", 2.0)
print("pclk 3", flush=True)
trial(s, "pclk3")

cmd(s, "pclk 1", 2.0)
cmd(s, "mode 1", 6.0)
print("mode 1 (sensor direct RGB565, no converter)", flush=True)
trial(s, "mode1")

cmd(s, "mode 0", 6.0)
cmd(s, "pda 1", 6.0)
print("restore mode0 + pda1", flush=True)
s.close()
print("ALL DONE", flush=True)
