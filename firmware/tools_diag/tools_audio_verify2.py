"""音频第二轮验证。"""
import serial, time, re

PORT = "COM3"


def open_clean(drain_s=4.0):
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


def cmd(s, c, wait=3.0, show=True, keys=None):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                t = re.sub(r"\x1b\[[0-9;]*m", "", line.decode("utf-8", "replace")).rstrip()
                out.append(t)
                if show and (keys is None or any(k in t for k in keys)):
                    print("   ", t[-95:])
        except Exception:
            pass
    return out


s = open_clean(4.0)
time.sleep(10.0)

print("=== 1) 内部 RAM ===", flush=True)
cmd(s, "mem", 3.0, show=True, keys=("剩余",))

print("\n=== 2) 默认 30dB 下的电平 (应该比之前高很多) ===", flush=True)
for i in range(6):
    cmd(s, "astat", 1.5, show=False)
    time.sleep(0.4)
print("   --- 原始日志 ---", flush=True)
cmd(s, "astat", 2.0, show=True, keys=("AUDIO",))

print("\n=== 3) 增益扫描 ===", flush=True)
for g in (12, 24, 30, 37):
    cmd(s, f"mgain {g}", 1.0, show=False)
    time.sleep(1.2)
    lines = cmd(s, "astat", 2.0, show=False)
    peak = rms = pl = pr = sp = None
    for l in lines:
        m = re.search(r"AUDIO peak=(\d+) rms=(\d+) L\(p=(\d+) r=(\d+)\)", l)
        if m:
            peak, rms, pl, pr = (int(m.group(i)) for i in range(1, 5))
        m2 = re.search(r"speaking=(\d)", l)
        if m2:
            sp = m2.group(1)
    print(f"    mgain={g:2d} dB -> peak={peak} rms={rms} L(p={pl} r={pr}) speaking={sp}", flush=True)

cmd(s, "mgain 30", 1.0, show=False)

print("\n=== 4) 测试音 ===", flush=True)
cmd(s, "tone", 3.0, show=True, keys=("测试音", "失败"))
time.sleep(4.0)
cmd(s, "astop", 2.0, show=False)

print("\n=== 5) 自环 axtest ===", flush=True)
cmd(s, "axtest", 32.0, show=True, keys=(
    "自环", "peak", "rms", "码率", "失败", "响应头", "通过", "响应"))
s.close()
