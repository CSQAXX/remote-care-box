"""屏幕闪屏诊断: 抓日志 + 健康指标 + 帧率。"""
import serial, time, re, urllib.request, json

PORT = "COM3"
HOST = "192.168.2.138"


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
                    print("   ", t[-100:])
        except Exception:
            pass
    return out


s = open_clean(3.0)
time.sleep(8.0)

print("=== 1) /health 全字段 ===", flush=True)
try:
    r = urllib.request.urlopen(f"http://{HOST}:8080/health", timeout=8)
    d = json.loads(r.read().decode())
    for k, v in d.items():
        print(f"    {k}: {v}")
except Exception as e:
    print("   FAIL:", e)

print("\n=== 2) 连续 6 秒 LCD 直绘帧率 ===", flush=True)
for i in range(6):
    lines = cmd(s, "stat", 1.5, show=False)
    for l in lines:
        if "直绘" in l or "帧" in l:
            print("   ", l[-100:])
    time.sleep(0.7)

print("\n=== 3) 串口里的错误/警告 (10 秒滚动窗口) ===", flush=True)
t0 = time.time()
seen = {}
while time.time() - t0 < 10:
    try:
        line = s.readline()
        if line:
            t = re.sub(r"\x1b\[[0-9;]*m", "", line.decode("utf-8", "replace")).rstrip()
            if any(k in t for k in ("E (", "W (", "错误", "失败", "超时", "timeout",
                                    "溢出", "重启", "panic", "abort", "堆")):
                key = t[12:70]
                seen[key] = seen.get(key, 0) + 1
    except Exception:
        pass
for k, v in sorted(seen.items(), key=lambda x: -x[1])[:25]:
    print(f"    x{v:3d}  {k}")

s.close()
