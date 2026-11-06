import serial, sys, time, re

port = "COM3"
s = serial.Serial(port, 115200, timeout=0.5)
# drain
t0 = time.time()
while time.time() - t0 < 1.0:
    try:
        s.readline()
    except Exception:
        pass

s.write(b"rawdump\n")
s.flush()

buf = bytearray()
state = 0
# state 0: look for b"RAWDUMP " in raw stream
needle = b"RAWDUMP "
found = False
while not found:
    c = s.read(1)
    if not c:
        break
    buf += c
    if buf.endswith(needle):
        found = True
if not found:
    print("no header found")
    sys.exit(1)
# read rest of header line
while not buf.endswith(b"\n"):
    c = s.read(1)
    if not c:
        break
    buf += c
hdr = bytes(buf)
print("HDR:", hdr)
m = re.search(rb"RAWDUMP (\d+) (\d+)", hdr)
if not m:
    print("bad header")
    sys.exit(1)
n = int(m.group(2))
buf = bytearray()
while len(buf) < n:
    chunk = s.read(min(65536, n - len(buf)))
    if not chunk:
        break
    buf += chunk
print("got", len(buf))
with open("raw_frame.bin", "wb") as f:
    f.write(buf)
# consume trailer
t0 = time.time()
while time.time() - t0 < 1.5:
    try:
        line = s.readline()
        if line and b"DONE" in line:
            break
    except Exception:
        pass
s.close()
print("saved raw_frame.bin")
