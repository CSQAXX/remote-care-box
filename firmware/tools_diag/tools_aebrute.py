"""暴力定位真正能停住 AE 的写法。

判别标准: 写入后 0x03/0x04 (曝光时间) 会不会【保持住不被传感器改回去】。
如果保持住 -> AEC 真的关了, 手动曝光生效。

候选:
  1) 0x22 的各种值 (0x00, 0x02..0x57 逐个单 bit 清除)
  2) 写 0x0a/0x0b/0x0c (GC0308 的 integration time) 看会不会被改回
  3) 写 0x10/0x11/0x12 同上
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


def read(s, reg):
    return parse(cmd(s, f"regs 0 {reg:02X} {reg:02X}", 1.0)).get(reg)


s = open_clean(3.0)
time.sleep(9.0)

print("=== A) 暴力试 0x22 的每个单 bit 清除 ===", flush=True)
print("  (判据: 写 0x03=AA 0x04=BB 后, 隔 1 秒回读, 不被改回 = AEC 真关了)")
for v in (0x57, 0x56, 0x55, 0x53, 0x50, 0x47, 0x17, 0x00,
          0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01, 0x52, 0x4F, 0x2F):
    cmd(s, f"wr 22 {v:02X}", 0.6)
    cmd(s, "wr 03 AA", 0.6)
    cmd(s, "wr 04 BB", 0.6)
    time.sleep(1.2)
    r3 = read(s, 0x03)
    r4 = read(s, 0x04)
    stick = (r3 == 0xAA and r4 == 0xBB)
    print(f"    0x22={v:02X} -> 0x03={('--' if r3 is None else format(r3,'02X'))} "
          f"0x04={('--' if r4 is None else format(r4,'02X'))}  "
          f"{'*** 保持住了! ***' if stick else '被改回'}", flush=True)
    cmd(s, "wr 22 57", 0.5)

print("\n=== B) 试 0x0a/0x0b/0x0c (integration time) ===", flush=True)
cmd(s, "wr 22 52", 0.6)
cmd(s, "wr 0a 11", 0.6)
cmd(s, "wr 0b 22", 0.6)
cmd(s, "wr 0c 33", 0.6)
time.sleep(1.5)
print(f"    0x0a={read(s,0x0a)} 0x0b={read(s,0x0b)} 0x0c={read(s,0x0c)} "
      f"(原 0xE8 0x02 0x88)")

print("\n=== C) 试 0x10/0x11/0x12 ===", flush=True)
cmd(s, "wr 10 44", 0.6)
cmd(s, "wr 11 55", 0.6)
cmd(s, "wr 12 66", 0.6)
time.sleep(1.5)
print(f"    0x10={read(s,0x10)} 0x11={read(s,0x11)} 0x12={read(s,0x12)} "
      f"(原 0x26 0x0D 0x2A)")
cmd(s, "wr 22 57", 0.5)
s.close()
