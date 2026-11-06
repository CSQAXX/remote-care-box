"""音频验证: 新固件刷完后全面体检。"""
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

print("=== 1) 内部 RAM (环形缓冲挪到 PSRAM 后应该明显变多) ===", flush=True)
cmd(s, "mem", 3.0, show=True, keys=("剩余",))

print("\n=== 2) ES7210 寄存器 ===", flush=True)
cmd(s, "adcreg", 5.0, show=True, keys=("0x", "增益", "ES7210"))

print("\n=== 3) 左右声道分开电平 (说话时应该有明显跳动) ===", flush=True)
for i in range(6):
    cmd(s, "astat", 1.5, show=False)
    time.sleep(0.4)

print("\n=== 4) 麦克风增益扫描 (这次用 mgain, 应该真的生效) ===", flush=True)
for g in (0, 12, 24, 30):
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

cmd(s, "mgain 12", 1.0, show=False)

print("\n=== 5) 测试音 (缓冲缩小后应该能分配成功) ===", flush=True)
cmd(s, "tone", 3.0, show=True, keys=("测试音", "失败", "tone"))
time.sleep(4.0)
cmd(s, "astop", 2.0, show=True, keys=("停止", "astop"))

print("\n=== 6) 自环测试 axtest ===", flush=True)
cmd(s, "axtest", 30.0, show=True, keys=(
    "自环", "AUDIO", "peak", "rms", "码率", "失败", "错误", "响应头", "通过"))
s.close()
