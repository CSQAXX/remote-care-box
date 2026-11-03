"""最终验收: 屏幕 + 音频一起确认。"""
import serial, time, re, urllib.request

PORT = "COM3"
HOST = "192.168.2.138"


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

print("=== 1) 视频链路 (摄像头) ===", flush=True)
cmd(s, "stat", 5.0, show=True, keys=("帧", "FPS", "分辨率", "画质", "模式", "OK", "错误"))

print("\n=== 2) LCD 显示链路 ===", flush=True)
cmd(s, "lcd2", 3.0, show=True, keys=("LCD", "屏", "缓冲", "SPI"))
# 【重要】诊断模式必须复位, 否则设备会一直停在 lcd2 合成图模式,
# 屏幕上就一直显示"一闪一闪的彩色条纹"。
# (2026-11-03 就是因为漏了这一步, 用户误以为硬件又坏了)
cmd(s, "lcd0", 3.0, show=True, keys=("LCD", "模式"))

print("\n=== 3) 内存 ===", flush=True)
cmd(s, "mem", 3.0, show=True, keys=("剩余",))

print("\n=== 4) 音频电平 (说话时应跳动) ===", flush=True)
for i in range(4):
    cmd(s, "astat", 1.5, show=False)
    time.sleep(0.4)
cmd(s, "astat", 2.0, show=True, keys=("AUDIO",))

print("\n=== 5) 音频端点 ===", flush=True)
for ep in ("/audio/level", "/health"):
    try:
        r = urllib.request.urlopen(f"http://{HOST}:8080{ep}", timeout=8)
        print(f"    {ep}: {r.status} {r.read().decode()[:200]}")
    except Exception as e:
        print(f"    {ep}: FAIL {e}")

s.close()
