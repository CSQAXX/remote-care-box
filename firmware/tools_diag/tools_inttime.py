"""测试 GC0308 的 "max/min integration time" 和 "max/min analog gain" 限制寄存器。

假设:
  0xd4 = 当前实际曝光时间 (行数), 在 11/24/29 之间跳
  0x03/0x04 = 曝光【上限】(AE 每次会改写它)
  0x53/0x54 = min/max integration time
  0x55/0x56 = min/max analog gain

如果把 max == min 写死, AE 就没有调节余地, 曝光应该被钉住。

判据: 写完之后连续观察 30 秒, 0xd4 变化次数应为 0。
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


def cmd(s, c, wait=1.0):
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


def d4(s):
    return parse(cmd(s, "regs 0 0 0d4 0d4", 0.9)).get(0xd4)


def watch(s, secs=28):
    t0 = time.time()
    prev = None
    ch = 0
    vals = set()
    while time.time() - t0 < secs:
        v = d4(s)
        if v is not None:
            vals.add(v)
            if prev is not None and v != prev:
                ch += 1
            prev = v
        time.sleep(0.3)
    return ch, sorted(vals)


s = open_clean(3.0)
time.sleep(9.0)

print("=== 基线 ===", flush=True)
ch, vals = watch(s, 24)
print(f"  0xd4 变化 {ch} 次, 值={[format(v,'02X') for v in vals]}\n", flush=True)

trials = [
    ("0x53=0x54=0x80, 0x55=0x56=0x80", ["wr 53 80", "wr 54 80", "wr 55 80", "wr 56 80"]),
    ("0x53=0x54=0x20", ["wr 53 20", "wr 54 20"]),
    ("0x55=0x56=0x14", ["wr 55 14", "wr 56 14"]),
    ("0x53=0x54=0x20 + 0x55=0x56=0x14", ["wr 53 20", "wr 54 20", "wr 55 14", "wr 56 14"]),
    ("0x53=0x54=0x1E (曝光=30)", ["wr 53 1E", "wr 54 1E"]),
    ("0x56=0x14 (gain 下限)", ["wr 56 14"]),
    ("0x94=0x00", ["wr 94 00"]),
    ("0x94=0x01", ["wr 94 01"]),
]

for name, cmds in trials:
    for c in cmds:
        cmd(s, c, 0.5)
    time.sleep(1.2)
    ch, vals = watch(s, 24)
    st = "*** 稳定! ***" if ch == 0 else f"还在变({ch})"
    print(f"  {name:34s} 0xd4={[format(v,'02X') for v in vals]}  {st}", flush=True)
    # 复位
    for c in ("wr 53 80", "wr 54 80", "wr 55 80", "wr 56 80", "wr 94 02"):
        cmd(s, c, 0.4)
s.close()
