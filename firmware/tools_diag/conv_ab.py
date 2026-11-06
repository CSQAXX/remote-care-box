import serial, time, sys, re

PORT = 'COM3'
s = serial.Serial(PORT, 115200, timeout=0.2)
buf = b''

def drain(sec):
    global buf
    end = time.time() + sec
    while time.time() < end:
        d = s.read(4096)
        if d: buf += d

def send(cmd, wait):
    s.write((cmd + '\r\n').encode())
    drain(wait)

def show(tag, since):
    global buf
    txt = buf[since:].decode('utf-8', 'replace')
    txt = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', txt)
    print("--- %s ---" % tag)
    for l in txt.splitlines():
        if any(k in l for k in ('QUAL', '噪点', '转换器', 'PROD', 'rst:', 'Guru', 'panic',
                                 'abort', 'fb_count', 'pixformat', 'YUV', 'RGB565', '失败')):
            print("   ", l.strip())

drain(3)

print("### A/B: 硬件转换器 ON vs OFF ###")
mark = len(buf)
send("q", 3)
show("BASELINE conv=1 (YUV422 + HW converter)", mark)

mark = len(buf)
send("conv 0", 12)     # re-init camera -> GC0308 native RGB565
send("q", 4)
show("conv=0 (GC0308 native RGB565, converter OFF)", mark)

mark = len(buf)
send("q", 4)
show("conv=0 repeat (stability check)", mark)

mark = len(buf)
send("conv 1", 12)     # back to converter
send("q", 4)
show("conv=1 again (converter ON)", mark)

mark = len(buf)
send("q", 4)
show("conv=1 repeat", mark)

s.close()
