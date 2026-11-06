"""系统排查: 怎么关掉 GC0308 的 8 秒周期 AE 震荡?

尝试矩阵 (每个方案观察 24 秒, 看 0xd4 是否还会变):
  0) 基线 (开机默认, AEC 已标称关闭)
  1) wr 22 52         (AAAA_EN: 关 AEC+AGC)
  2) wr 22 50         (再关 AWB)
  3) wr 22 42         (AAAA_EN bit1=AWB off, bit0/2 off -> 01000010)
  4) wr 22 52 + wr 03/04 固定曝光
  5) wr d3 00         (AEC_TARGET_Y = 0, 让 AE 认为永远够亮)
  6) wr d3 FF         (AEC_TARGET_Y = 255, 让 AE 认为永远不够亮)
  7) wr 22 52 + wr 94 xx  (0x94 是 AE 相关开关)
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


def cmd(s, c, wait=1.5):
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


def watch(s, secs=22):
    """观察 secs 秒, 返回 0xd4 的变化次数和出现的值"""
    t0 = time.time()
    prev = None
    changes = 0
    vals = set()
    while time.time() - t0 < secs:
        d4 = parse(cmd(s, "regs 0xd4 0xd4", 1.0)).get(0xd4)
        if d4 is not None:
            vals.add(d4)
            if prev is not None and d4 != prev:
                changes += 1
            prev = d4
        time.sleep(0.25)
    return changes, sorted(vals)


s = open_clean(3.0)
time.sleep(7.0)

trials = [
    ("0) 基线(开机默认)", []),
    ("1) wr 22 52", ["wr 22 52"]),
    ("2) wr 22 50", ["wr 22 50"]),
    ("3) wr 22 42", ["wr 22 42"]),
    ("4) wr 22 52 + 固定曝光0x1E0", ["wr 22 52", "wr 03 01", "wr 04 E0"]),
    ("5) wr d3 00", ["wr d3 00"]),
    ("6) wr d3 FF", ["wr d3 FF"]),
    ("7) wr 22 52 + wr d3 48", ["wr 22 52", "wr d3 48"]),
    ("8) wr 22 52 + wr 94 02", ["wr 22 52", "wr 94 02"]),
]

for name, cmds in trials:
    for c in cmds:
        cmd(s, c, 0.8)
    time.sleep(1.5)
    ch, vals = watch(s, 22)
    status = "稳定!!" if ch == 0 else f"还在变({ch}次)"
    print(f"  {name:32s} 0xd4值={['%02X'%v for v in vals]}  {status}", flush=True)
    # 复位回基线
    cmd(s, "wr 22 57", 0.8)
    cmd(s, "wr d3 48", 0.8)
s.close()
