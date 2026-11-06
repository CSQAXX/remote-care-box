import serial, sys, time

cmd = sys.argv[1] if len(sys.argv) > 1 else "evtest"
s = serial.Serial("COM3", 115200, timeout=0.2)
time.sleep(0.5)
s.reset_input_buffer()
s.write((cmd + "\n").encode("utf-8"))
time.sleep(1.5)
out = s.read_all().decode("utf-8", errors="replace")
print(out)
s.close()
