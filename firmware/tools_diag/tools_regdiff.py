"""全寄存器扫描: 连续两次 dump 0x00-0xff, 找出到底哪些寄存器在变。

这是定位"亮度跳动"来源的决定性手段:
  - 某个曝光/增益寄存器在变 -> 传感器 AE 在工作
  - 一个都不变 -> 不是寄存器层面的原因, 要往别处查
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


def cmd(s, c, wait=2.0):
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


s = open_clean(3.0)
time.sleep(6.0)

snaps = []
for i in range(6):
    r = parse(cmd(s, "regs 0 255", 25.0))
    snaps.append(r)
    print(f"  轮{i}: 读到 {len(r)} 个寄存器", flush=True)
    time.sleep(0.5)

common = set(snaps[0].keys())
for s2 in snaps[1:]:
    common &= set(s2.keys())

print(f"\n=== 在 {len(snaps)} 次快照之间发生变化的寄存器 (共 {len(common)} 个可比) ===")
changed = []
for reg in sorted(common):
    vals = [sn[reg] for sn in snaps]
    if len(set(vals)) > 1:
        changed.append((reg, vals))
if not changed:
    print("  【没有一个寄存器在变】")
else:
    for reg, vals in changed:
        print(f"  0x{reg:02x}: " + " ".join(f"{v:02X}" for v in vals))
s.close()
