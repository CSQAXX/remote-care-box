"""验证 M2 按键事件链路现状: /event 是否有事件、类型是什么。"""
import serial, time, re, urllib.request

HOST = "192.168.2.138"


def get_event():
    try:
        r = urllib.request.urlopen(f"http://{HOST}:8080/event", timeout=5)
        return r.read().decode()
    except Exception as e:
        return f"FAIL {e}"


s = serial.Serial("COM3", 115200, timeout=0.2)
try:
    s.dtr = False
    s.rts = False
except Exception:
    pass
time.sleep(2)
t0 = time.time()
while time.time() - t0 < 3:
    try:
        s.readline()
    except Exception:
        pass

print("=== 1) 队列清空测试 ===")
for _ in range(6):
    print(f"    /event -> {get_event()}")

print("\n=== 2) 等待用户按 BOOT 键 (GPIO0) ===", flush=True)
print("    (请按板子上的 BOOT 键, 20 秒内有效)", flush=True)

t0 = time.time()
got = None
while time.time() - t0 < 20:
    r = get_event()
    if '"none"' not in r:
        got = r
        print(f"    >>> 收到事件: {r}")
        break
    time.sleep(0.4)

if not got:
    print("    (超时, 没检测到按键事件)")

print("\n=== 3) 串口侧按键日志 ===")
t0 = time.time()
while time.time() - t0 < 3:
    try:
        l = s.readline()
        if l:
            t = re.sub(r"\x1b\[[0-9;]*m", "", l.decode("utf-8", "replace")).rstrip()
            if any(k in t for k in ("按键", "事件", "BOOT")):
                print("   ", t[-90:])
    except Exception:
        pass

s.close()
