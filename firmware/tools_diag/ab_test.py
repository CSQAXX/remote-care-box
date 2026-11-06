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
    lines = [l for l in txt.splitlines()
             if ('QUAL' in l or '噪点' in l or '转换器' in l or 'PSRAM DMA' in l
                 or 'PROD' in l or '直绘' in l or 'LCD 显示路径' in l
                 or 'WDT' in l or 'rst:' in l or 'abort' in l or 'panic' in l
                 or 'ERROR' in l or '失败' in l or '重启' in l)]
    print("--- %s ---" % tag)
    for l in lines[-14:]:
        print("   ", l.strip())

drain(3)
seq = [
    ("BASELINE (noise on, converter on, pda on, wifi on)", ["q"], 4),
    ("Wi-Fi OFF",                                   ["wifioff", "q"], 6),
    ("Wi-Fi back ON",                               ["wifix", "q"], 6),
    ("PSRAM DMA OFF (DVP -> DRAM staging)",        ["pda 0", "q"], 8),
    ("PSRAM DMA ON",                               ["pda 1", "q"], 8),
    ("LCD slot-copy path (not direct draw)",       ["ldirect", "q"], 6),
    ("LCD direct draw (back)",                     ["ldirect", "q"], 6),
]
for tag, cmds, wait in seq:
    mark = len(buf)
    for c in cmds:
        send(c, wait if c.startswith('pda') or c in ('wifioff','wifix') else 2.5)
    show(tag, mark)

s.close()
