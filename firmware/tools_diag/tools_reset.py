import serial, sys, time
port = "COM3"
s = serial.Serial(port, 115200, timeout=0.3)
# toggle DTR/RTS to reset the board (ESP32 auto-reset circuit)
try:
    s.dtr = False
    s.rts = True
    time.sleep(0.15)
    s.rts = False
except Exception as e:
    print("reset toggle failed:", e)
time.sleep(0.4)
t0 = time.time()
while time.time() - t0 < 25:
    try:
        line = s.readline()
        if line:
            print(line.decode("utf-8", "replace").rstrip())
    except Exception:
        pass
s.close()
