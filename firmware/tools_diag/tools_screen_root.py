"""确认闪屏根因: LCD 模式 + 帧率 + WiFi 干扰。"""
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


def health():
    try:
        r = urllib.request.urlopen(f"http://{HOST}:8080/health", timeout=6)
        return json.loads(r.read().decode())
    except Exception as e:
        return {"err": str(e)}


s = open_clean(3.0)
time.sleep(9.0)

KEYS = ("lcd_frames", "lcd_draw_us", "cam_frames", "cam_get_us",
        "torn_frames", "g2_bad", "bad_rows", "hi_jumps", "lo_jumps")

print("=== 1) 当前 LCD 模式 + 帧率 ===", flush=True)
cmd(s, "stat", 4.0, show=True, keys=("LCD", "PROD", "模式", "直绘"))
h = health()
print("   ", "  ".join(f"{k}={h.get(k)}" for k in KEYS))

print("\n=== 2) 切 lcd1 固定图样 (不碰摄像头, 每秒重画) ===", flush=True)
cmd(s, "lcd1", 2.0, show=True)
time.sleep(4.0)
for i in range(4):
    h = health()
    print(f"    [{i}] " + "  ".join(f"{k}={h.get(k)}" for k in KEYS), flush=True)
    time.sleep(2.0)

print("\n=== 3) 关 WiFi (验证 PSRAM 总线争用) ===", flush=True)
cmd(s, "wifioff", 3.0, show=True)
time.sleep(4.0)
for i in range(4):
    h = health()
    print(f"    [{i}] " + "  ".join(f"{k}={h.get(k)}" for k in KEYS), flush=True)
    time.sleep(2.0)

print("\n=== 4) 恢复 WiFi + 摄像头模式 ===", flush=True)
cmd(s, "wifix", 2.0, show=True)
cmd(s, "lcd0", 2.0, show=True)
time.sleep(6.0)
for i in range(4):
    h = health()
    print(f"    [{i}] " + "  ".join(f"{k}={h.get(k)}" for k in KEYS), flush=True)
    time.sleep(2.0)

s.close()
