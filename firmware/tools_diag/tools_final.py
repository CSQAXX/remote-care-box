import serial, time, re, sys

port = "COM3"
DEV = "D:\\Software\\kanhu\\remote-care-box\\firmware"

def open_clean(drain_s=3.0):
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
                if show:
                    print("   ", txt[-100:])
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

print("=== 1) 开机默认配置确认 ===", flush=True)
for l in cmd(s, "q", 3.0):
    if "QUAL" in l or "hi_energy" in l:
        print("   ", l[-110:])

print("\n=== 2) 彩条静止帧间差异 (关键指标, 应该接近 0) ===", flush=True)
cmd(s, "cbar 1", 1.5)
time.sleep(3.0)
for i in (1, 2, 3):
    n = rawdump(s, f"fin_cb{i}.bin")
    print(f"   fin_cb{i}: {n}", flush=True)
    time.sleep(0.5)
cmd(s, "cbar 0", 1.0)

print("\n=== 3) 真实场景帧间差异 (归一化曝光后) ===", flush=True)
time.sleep(1.5)
for i in (1, 2, 3):
    n = rawdump(s, f"fin_sc{i}.bin")
    print(f"   fin_sc{i}: {n}", flush=True)
    time.sleep(0.4)

print("\n=== 4) 帧率 ===", flush=True)
time.sleep(4)
for l in cmd(s, "stat", 2.0):
    if "cam_frames" in l:
        print("   ", l[-90:])
s.close()
