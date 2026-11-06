"""
验证需求文档 §3.8 的求助按键闭环。

需要人工配合: 按板子上的 BOOT 键。
  - 短按 (很快松开) = 求助 -> 屏幕「已通知家人」+ /event type=button
  - 长按 (按住 2 秒) = 撤销 -> 屏幕「求助已取消」+ /event type=cancel
"""
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

# 清空队列
for _ in range(8):
    get_event()

print("=" * 60)
print("请按 BOOT 键【短按】 (1 秒内松开)")
print("=" * 60, flush=True)
got = None
t0 = time.time()
while time.time() - t0 < 25:
    r = get_event()
    if '"none"' not in r:
        got = r
        break
    time.sleep(0.3)

print(f"  /event -> {got}")
if got and '"button"' in got:
    print("  [OK] 短按 -> type=button (求助)")
elif got and '"cancel"' in got:
    print("  [??] 得到了 cancel, 说明被判定成长按")
else:
    print("  [FAIL] 超时, 没检测到短按事件")

# 收集串口日志
print("\n串口日志:")
t0 = time.time()
while time.time() - t0 < 2:
    try:
        l = s.readline()
        if l:
            t = re.sub(r"\x1b\[[0-9;]*m", "", l.decode("utf-8", "replace")).rstrip()
            if any(k in t for k in ("按键", "提示", "求助")):
                print("  ", t[-95:])
    except Exception:
        pass

print()
print("=" * 60)
print("现在请按住 BOOT 键 2 秒以上再松开 (长按 = 撤销)")
print("=" * 60, flush=True)
got2 = None
t0 = time.time()
while time.time() - t0 < 25:
    r = get_event()
    if '"none"' not in r:
        got2 = r
        break
    time.sleep(0.3)

print(f"  /event -> {got2}")
if got2 and '"cancel"' in got2:
    print("  [OK] 长按 -> type=cancel (撤销)")
elif got2 and '"button"' in got2:
    print("  [??] 得到了 button, 说明被判定成短按")
else:
    print("  [FAIL] 超时, 没检测到长按事件")

print("\n串口日志:")
t0 = time.time()
while time.time() - t0 < 2:
    try:
        l = s.readline()
        if l:
            t = re.sub(r"\x1b\[[0-9;]*m", "", l.decode("utf-8", "replace")).rstrip()
            if any(k in t for k in ("按键", "提示", "求助")):
                print("  ", t[-95:])
    except Exception:
        pass

s.close()
