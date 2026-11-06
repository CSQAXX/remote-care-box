import serial, sys, time
port = "COM3"
cmd = sys.argv[1]
dur = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
s = serial.Serial(port, 115200, timeout=0.3)
# drain
t0 = time.time()
while time.time() - t0 < 1.0:
    try:
        s.readline()
    except Exception:
        pass
s.write((cmd + "\n").encode())
s.flush()
t0 = time.time()
while time.time() - t0 < dur:
    try:
        line = s.readline()
        if line:
            sys.stdout.write(line.decode("utf-8", "replace"))
            sys.stdout.flush()
    except Exception:
        pass
s.close()
