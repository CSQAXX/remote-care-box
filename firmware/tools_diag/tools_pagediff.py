"""扫描 page1/page2/page3 的寄存器, 找出 AE 判决真正写在哪。

每页扫两次, 间隔足够让 8 秒周期走完一半, 找出在变的寄存器。
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
time.sleep(9.0)

for page in (1, 2, 3):
    print(f"\n=== page{page} 扫描 (两轮, 间隔 5 秒) ===", flush=True)
    a = parse(cmd(s, f"regs {page} 0 255", 26.0))
    print(f"    第一轮读到 {len(a)} 个", flush=True)
    time.sleep(5.0)
    b = parse(cmd(s, f"regs {page} 0 255", 26.0))
    common = set(a.keys()) & set(b.keys())
    changed = [(r, a[r], b[r]) for r in sorted(common) if a[r] != b[r]]
    if changed:
        for r, x, y in changed:
            print(f"    0x{r:02x}: 0x{x:02X} -> 0x{y:02X}   <== 在变")
    else:
        print(f"    (page{page} 共 {len(common)} 个可比, 无变化)")
s.close()
