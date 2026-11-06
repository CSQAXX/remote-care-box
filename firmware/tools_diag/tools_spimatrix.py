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

def trial(s, tag):
    """真实场景抓两帧 (不用彩条, 排除彩条本身滚动)"""
    time.sleep(1.0)
    rawdump(s, f"u_{tag}_a.bin")
    time.sleep(0.5)
    rawdump(s, f"u_{tag}_b.bin")
    print(f"  [{tag}] done", flush=True)

s = open_clean(3.0)
# 恢复基准: pda 1 + pclk 1 (最坏情况)
cmd(s, "pclk 1", 2.0)
cmd(s, "pda 1", 8.0)

for clk in (60000000, 40000000, 20000000, 10000000):
    cmd(s, f"spiclk {clk}", 6.0)
    trial(s, f"spi{clk//1000000}")

cmd(s, "spiclk 60000000", 6.0)
s.close()
print("ALL DONE", flush=True)
