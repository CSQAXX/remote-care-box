import serial, time, sys, re

PORT = 'COM3'
CMDS = sys.argv[1] if len(sys.argv) > 1 else ''
WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

s = serial.Serial(PORT, 115200, timeout=0.2)
buf = b''
end = time.time() + 1.0
while time.time() < end:
    d = s.read(4096)
    if d: buf += d
s.reset_input_buffer()

for cmd in [c for c in CMDS.split('|') if c]:
    s.write((cmd + '\r\n').encode())
    t0 = time.time()
    while time.time() - t0 < 2.5:
        d = s.read(4096)
        if d: buf += d

t0 = time.time()
while time.time() - t0 < WAIT:
    d = s.read(4096)
    if d: buf += d

txt = buf.decode('utf-8', 'replace')
txt = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', txt)
sys.stdout.buffer.write(txt.encode('utf-8', 'replace'))
s.close()
