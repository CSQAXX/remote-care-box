"""切回摄像头模式, 然后观察健康指标变化。"""
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
time.sleep(8.0)

print("=== 1) 当前状态 (可能是 lcd2 合成图模式) ===", flush=True)
h = health()
for k in ("lcd_direct", "lcd_frames", "lcd_draw_us", "cam_frames",
          "torn_frames", "noise_rows", "bad_rows", "hi_jumps", "lo_jumps"):
    print(f"    {k}: {h.get(k)}")

print("\n=== 2) 切回摄像头模式 lcd0 ===", flush=True)
cmd(s, "lcd0", 2.0, show=True)
time.sleep(3.0)

print("\n=== 3) 摄像头模式下的健康指标 (每 3 秒采样) ===", flush=True)
KEYS = ("lcd_frames", "lcd_draw_us", "cam_frames", "torn_frames",
        "noise_rows", "noise_avg", "bad_rows", "hi_jumps", "lo_jumps",
        "hi_energy", "lo_energy", "cam_get_us", "rgb_copy_us")
for i in range(6):
    h = health()
    vals = "  ".join(f"{k}={h.get(k)}" for k in KEYS)
    print(f"    [{i}] {vals}", flush=True)
    time.sleep(3.0)

print("\n=== 4) 串口错误扫描 (8 秒) ===", flush=True)
t0 = time.time()
seen = {}
while time.time() - t0 < 8:
    try:
        line = s.readline()
        if line:
            t = re.sub(r"\x1b\[[0-9;]*m", "", line.decode("utf-8", "replace")).rstrip()
            if any(k in t for k in ("E (", "W (", "错误", "失败", "超时", "堆", "panic")):
                key = t[12:70]
                seen[key] = seen.get(key, 0) + 1
    except Exception:
        pass
for k, v in sorted(seen.items(), key=lambda x: -x[1])[:20]:
    print(f"    x{v:3d}  {k}")
if not seen:
    print("    (无错误)")

s.close()
