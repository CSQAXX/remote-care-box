import serial, time, sys, re

PORT = sys.argv[1] if len(sys.argv) > 1 else 'COM3'
CMD = sys.argv[2] if len(sys.argv) > 2 else ''

s = serial.Serial(PORT, 115200, timeout=0.2)
buf = b''
end = time.time() + (float(sys.argv[3]) if len(sys.argv) > 3 else 3.0)
# drain
while time.time() < end:
    d = s.read(4096)
    if d:
        buf += d
s.reset_input_buffer()

if CMD:
    s.write((CMD + '\r\n').encode())
    t0 = time.time()
    while time.time() - t0 < 4.0:
        d = s.read(4096)
        if d:
            buf += d

txt = buf.decode('utf-8', 'replace')
# strip ANSI
txt = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', txt)
print(txt[-12000:])
s.close()
