"""音频诊断:
1) 麦克风增益扫描 (0 -> 37.5 dB), 看电平是否线性跟随
   - 线性跟随 -> 只是增益不够
   - 完全不动   -> 麦克风链路本身没通
2) 读 ES7210 关键寄存器
3) 扬声器测试音
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


def cmd(s, c, wait=2.0, show=True, keys=None):
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
                    print("   ", t[-92:])
        except Exception:
            pass
    return out


s = open_clean(3.0)
time.sleep(8.0)

print("=== 1) 麦克风增益扫描 (每档采样 2 秒) ===", flush=True)
print("    (注意: 'gain' 命令名和摄像头增益冲突, 这里改用音量测试)", flush=True)
for g in (0, 6, 12, 18, 24, 30, 36):
    cmd(s, f"gain {g}", 1.2, show=False)
    time.sleep(1.0)
    lines = cmd(s, "astat", 2.2, show=False)
    peak = rms = sp = None
    for l in lines:
        m = re.search(r"AUDIO peak=(\d+) rms=(\d+) samples=\d+ frames=\d+ speaking=(\d)", l)
        if m:
            peak, rms, sp = int(m.group(1)), int(m.group(2)), int(m.group(3))
    print(f"    gain={g:2d} dB -> peak={peak} rms={rms} speaking={sp}", flush=True)

cmd(s, "gain 12", 1.0, show=False)

print("\n=== 2) 恢复默认增益后测 10 秒 (对着麦克风说话) ===", flush=True)
for i in range(5):
    cmd(s, "astat", 1.5, show=False)
    time.sleep(0.6)

print("\n=== 3) 扬声器: 启动测试音 ===", flush=True)
cmd(s, "tone", 2.0, show=True, keys=("测试音", "tone", "AUDIO", "tone_task"))

print("\n=== 4) 堆内存 ===", flush=True)
cmd(s, "mem", 3.0, show=True, keys=("剩余", "heap", "Heap", "内部"))
s.close()
