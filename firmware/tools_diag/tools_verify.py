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

def cmd(s, c, wait=2.0, show=False):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                txt = line.decode("utf-8", "replace").rstrip()
                out.append(txt)
                if show and "kanhu" in txt:
                    print("   ", txt[-90:])
        except Exception:
            pass
    return out

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
print("== 应用 pda 0 ==", flush=True)
cmd(s, "pda 0", 8.0, show=True)
print("== 应用 pclk 2 ==", flush=True)
cmd(s, "pclk 2", 3.0, show=True)

print("== 彩条 3 帧 ==", flush=True)
cmd(s, "cbar 1", 1.5)
time.sleep(3.0)
for i in (1, 2, 3):
    n = rawdump(s, f"win_cb{i}.bin")
    print(f"   win_cb{i}: {n}", flush=True)
    time.sleep(0.5)
cmd(s, "cbar 0", 1.0)

print("== 真实场景 3 帧 ==", flush=True)
time.sleep(1.5)
for i in (1, 2, 3):
    n = rawdump(s, f"win_sc{i}.bin")
    print(f"   win_sc{i}: {n}", flush=True)
    time.sleep(0.4)

print("== 帧率 / 质量 ==", flush=True)
for l in cmd(s, "q", 3.0):
    if "QUAL" in l or "hi_energy" in l:
        print("   ", l[-110:])
time.sleep(3)
for l in cmd(s, "stat", 2.0):
    if "cam_frames" in l:
        print("   ", l[-90:])
s.close()
