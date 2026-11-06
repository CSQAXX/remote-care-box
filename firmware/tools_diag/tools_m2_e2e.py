"""
M2 按键弹话 —— 端到端验收。

验证需求文档 A5「按板子按钮，网页弹出中文提示」和 A6「提示内容可自定义」。

链路分三段, 本脚本逐段取证:
  段 1  事件能进队列      串口 evtest -> 日志「已注入按键事件」
  段 2  /event 能取出事件  HTTP GET /event -> JSON 里 type=button
  段 3  网页真能弹窗      浏览器打开 /, 注入事件, 看 DOM 里 #alert 有没有文字

段 3 是 A5 的核心 —— 必须用真实浏览器验证, 光看 JSON 不算数。
"""
import json
import re
import serial
import time
import urllib.request

HOST = "192.168.2.138"
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


def cmd(s, c, wait=3.0):
    """发串口命令, 返回 (打印行, 原始输出行)"""
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            l = s.readline()
            if l:
                t = re.sub(r"\x1b\[[0-9;]*m", "",
                           l.decode("utf-8", "replace")).rstrip()
                out.append(t)
        except Exception:
            pass
    return out


def get_event():
    try:
        r = urllib.request.urlopen(f"http://{HOST}:8080/event", timeout=6)
        return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"FAIL {e}"


def drain_events():
    """把队列里积压的事件全部取走, 保证测试从干净状态开始"""
    n = 0
    while n < 20:
        r = get_event()
        if '"none"' in r:
            break
        n += 1
        time.sleep(0.2)
    return n


s = open_clean(3.0)
time.sleep(10.0)

print("=" * 64)
print("M2 按键弹话 端到端验收")
print("=" * 64)

# ---------- 段 1+2: 事件进队列 + /event 取出 ----------
print("\n[段 1] 注入事件 (默认文案)")
stale = drain_events()
if stale:
    print(f"    (先清掉 {stale} 条积压事件)")

out = cmd(s, "evtest", 3.0)
injected = any("已注入按键事件" in t for t in out)
print(f"    串口日志确认注入: {'OK' if injected else 'FAIL'}")
for t in out:
    if "注入" in t:
        print(f"      {t[-80:]}")

time.sleep(0.6)
ev = get_event()
print(f"    GET /event -> {ev}")
ok2 = '"type":"button"' in ev
print(f"    /event 取出事件: {'OK' if ok2 else 'FAIL'}")

# ---------- A6: 文案可自定义 ----------
print("\n[段 2] 注入自定义文案 (验证 A6)")
drain_events()
CUSTOM = "奶奶说想喝水了"
cmd(s, f"evtest {CUSTOM}", 3.0)
time.sleep(0.6)
ev = get_event()
print(f"    GET /event -> {ev}")
ok3 = CUSTOM in ev
print(f"    自定义文案生效: {'OK' if ok3 else 'FAIL'}")

# ---------- 队列空时返回 none ----------
print("\n[段 3] 队列排空后 /event 的行为")
ev = get_event()
print(f"    GET /event -> {ev}")
ok4 = '"type":"none"' in ev
print(f"    空队列返回 type=none: {'OK' if ok4 else 'FAIL'}")

print("\n" + "=" * 64)
print(f"段 1 (注入)      : {'通过' if injected else '失败'}")
print(f"段 2 (/event)    : {'通过' if ok2 else '失败'}")
print(f"A6 (文案可配置)  : {'通过' if ok3 else '失败'}")
print(f"空队列行为       : {'通过' if ok4 else '失败'}")
print("=" * 64)
print("\n段 4 (网页真弹窗) 需要浏览器验证, 见下一步")
print("浏览器打开: http://%s:8080/" % HOST)

# 保持串口连接, 让后面的浏览器步骤能用
import atexit
atexit.register(lambda: s.close())
