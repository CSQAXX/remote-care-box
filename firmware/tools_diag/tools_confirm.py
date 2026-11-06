import serial, time, re

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

def trial(s, tag, n=4):
    time.sleep(1.5)
    for i in range(n):
        rawdump(s, f"w_{tag}_{i}.bin")
        time.sleep(0.45)
    print(f"  [{tag}] done", flush=True)

s = open_clean(3.0)

# 1) 最坏基准: pda1 + pclk1
cmd(s, "pclk 1", 2.0)
cmd(s, "pda 1", 8.0)
trial(s, "worst")

# 2) 候选: pda0 + pclk2
cmd(s, "pda 0", 8.0)
cmd(s, "pclk 2", 2.0)
trial(s, "best")

# 3) 候选复测: 再切回最坏, 再切回最好
cmd(s, "pclk 1", 2.0)
cmd(s, "pda 1", 8.0)
trial(s, "worst2")
cmd(s, "pda 0", 8.0)
cmd(s, "pclk 2", 2.0)
trial(s, "best2")

s.close()
print("ALL DONE", flush=True)
