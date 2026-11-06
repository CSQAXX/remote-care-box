import serial, sys, time
port = "COM3"
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
s = serial.Serial(port, 115200, timeout=0.3)
t0 = time.time()
out = []
while time.time() - t0 < dur:
    try:
        line = s.readline()
        if line:
            out.append(line.decode("utf-8", "replace"))
    except Exception:
        pass
s.close()
for l in out:
    print(l.rstrip())
