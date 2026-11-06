import serial, sys, time
port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
dur = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
s = serial.Serial(port, 115200, timeout=0.5)
t0 = time.time()
try:
    while time.time() - t0 < dur:
        line = s.readline()
        if line:
            sys.stdout.write(line.decode("utf-8", "replace"))
            sys.stdout.flush()
finally:
    s.close()
