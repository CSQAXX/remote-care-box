import serial, sys, time, re

port = "COM3"

def open_clean(drain_s=1.5):
    s = serial.Serial(port, 115200, timeout=0.2)
    # 关键: pyserial 打开串口时默认拉高 DTR/RTS, 对 ESP32 的 EN/IO0
    # 来说 = 复位。打开后立刻拉低, 避免每次都重启板子。
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
                txt = line.decode("utf-8", "replace").rstrip()
                lines.append(txt)
                if "DONE" in txt:
                    break
        except Exception:
            pass
    return lines

def rawdump(s, path="raw_frame.bin"):
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

if __name__ == "__main__":
    s = open_clean(2.0)
    print("--- cbar 1 ---")
    for l in send(s, "cbar 1", 2.5):
        print("  ", l)
    time.sleep(1.5)
    print("--- rawdump ---")
    n = rawdump(s)
    print("dumped bytes:", n)
    s.close()
