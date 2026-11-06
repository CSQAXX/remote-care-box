"""聚焦扫描 0x53-0x59: 找出能把 AE 钉住的 min/max 限制寄存器。

已知线索:
  - 写 0x55=0x56=0x14 后, 0xd4 只变 2 次/24s (基线 12-14 次/24s) -> 明显变稳
  - 说明这一对寄存器确实在限制 AE 的调节范围
  - 目标: 让 min == max, AE 就没有调节余地了
"""
import serial, time, re

PORT = "COM3"


def open_clean(drain_s=3.0):
    s = serial.Serial(PORT, 115200, timeout=0.2)
    try:
        s.dtr = False
        s.rts = False
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < drain_s:
        try:
            s.readline()
        except Exception:
            pass
    return s


def cmd(s, c, wait=0.8):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                out.append(line.decode("utf-8", "replace"))
        except Exception:
            pass
    return out


def parse(lines):
    out = {}
    for l in lines:
        m = re.search(r"REG 0x([0-9a-fA-F]{2}) = 0x([0-9a-fA-F]{2})", l)
        if m:
            out[int(m.group(1), 16)] = int(m.group(2), 16)
    return out


def watch(s, secs=20):
    t0 = time.time()
    prev = None
    ch = 0
    vals = set()
    while time.time() - t0 < secs:
        v = parse(cmd(s, "regs 0 0 0d4 0d4", 0.7)).get(0xd4)
        if v is not None:
            vals.add(v)
            if prev is not None and v != prev:
                ch += 1
            prev = v
        time.sleep(0.2)
    return ch, sorted(vals)


s = open_clean(3.0)
time.sleep(9.0)

print("=== 基线 ===", flush=True)
ch, vals = watch(s, 20)
print(f"  变化 {ch} 次, 0xd4={[format(v,'02X') for v in vals]}\n", flush=True)

RESET = ["wr 53 80", "wr 54 80", "wr 55 80", "wr 56 80",
         "wr 57 80", "wr 58 80", "wr 59 80"]

trials = [
    ("56=57=00", ["wr 56 00", "wr 57 00"]),
    ("58=59=00", ["wr 58 00", "wr 59 00"]),
    ("56=57=08", ["wr 56 08", "wr 57 08"]),
    ("58=59=08", ["wr 58 08", "wr 59 08"]),
    ("57=59=00", ["wr 57 00", "wr 59 00"]),
    ("56=58=00", ["wr 56 00", "wr 58 00"]),
    ("全部=08", ["wr 56 08", "wr 57 08", "wr 58 08", "wr 59 08"]),
    ("55=56=08 57=58=08", ["wr 55 08", "wr 56 08", "wr 57 08", "wr 58 08"]),
]

for name, cmds in trials:
    for c in RESET:
        cmd(s, c, 0.3)
    for c in cmds:
        cmd(s, c, 0.4)
    time.sleep(1.0)
    ch, vals = watch(s, 20)
    st = "*** 稳定! ***" if ch == 0 else f"变{ch}次"
    print(f"  {name:26s} 0xd4={[format(v,'02X') for v in vals]}  {st}", flush=True)

for c in RESET:
    cmd(s, c, 0.3)
s.close()
