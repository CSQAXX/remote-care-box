"""
M2 端到端验收 —— 用 /notify 回执做硬证据。

关键洞察:
  网页拿到事件并弹窗之后, 会 POST /notify 告诉板子"我收到了"。
  所以只要串口出现「电脑已确认收到: <文案>」, 就证明:
    事件队列 -> /event -> 网页 JS 轮询 -> 弹窗 -> POST /notify
  整条链路都走通了 —— 而不只是接口返回了 JSON。

这也顺带验证了需求文档里的 /notify 约定 (附录 8.1)。

用法:
  python tools_m2_notify_e2e.py            # 需要浏览器页面已打开 http://<IP>:8080/
"""
import re
import serial
import time

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


def read_log(s, seconds, keys):
    """读一段时间的串口日志, 只保留含指定关键字的行"""
    hits = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            l = s.readline()
            if l:
                t = re.sub(r"\x1b\[[0-9;]*m", "",
                           l.decode("utf-8", "replace")).rstrip()
                if any(k in t for k in keys):
                    hits.append(t)
        except Exception:
            pass
    return hits


s = open_clean(3.0)
time.sleep(1.0)

print("=" * 66)
print("M2 端到端验收 —— 以 /notify 回执为硬证据")
print("=" * 66)
print(f"目标设备: http://{HOST}:8080/")
print("请确保浏览器已打开该页面 (它每 500ms 轮询 /event)\n")

CASES = [
    ("默认文案 (BUTTON_MESSAGE)", None),
    ("自定义文案 (验证 A6)", "奶奶说想喝水了"),
    ("第二个自定义文案", "爸爸在厨房"),
]

results = []
for label, custom in CASES:
    print(f"--- {label} ---", flush=True)
    cmd = "evtest" if custom is None else f"evtest {custom}"
    s.write((cmd + "\n").encode())
    s.flush()

    # 等 /notify 回执 (网页弹窗后会 POST 回来)
    hits = read_log(s, 6.0, ("已注入按键事件", "电脑已确认收到", "取出一个按键事件"))
    for h in hits:
        print(f"    {h[-95:]}")

    got_inject = any("已注入按键事件" in h for h in hits)
    got_taken = any("取出一个按键事件" in h for h in hits)
    got_notify = any("电脑已确认收到" in h for h in hits)
    notify_text = ""
    for h in hits:
        if "电脑已确认收到" in h:
            notify_text = h.split("电脑已确认收到:")[-1].strip()

    expect = custom if custom else "老人按了求助按钮"
    text_ok = (expect in notify_text) if notify_text else False

    print(f"    注入: {'OK' if got_inject else 'FAIL'} | "
          f"/event 出队: {'OK' if got_taken else 'FAIL'} | "
          f"/notify 回执: {'OK' if got_notify else 'FAIL'} | "
          f"文案一致: {'OK' if text_ok else 'FAIL'}")
    print(f"    回执内容: {notify_text if notify_text else '(无)'}")
    results.append((label, got_inject, got_taken, got_notify, text_ok))
    print()

print("=" * 66)
print("汇总")
print("=" * 66)
allok = True
for label, a, b, c, d in results:
    ok = a and b and c and d
    allok = allok and ok
    print(f"  {label:28s} {'通过' if ok else '失败'}")
print()
print("A5 (按按钮网页弹中文): " + ("通过" if allok else "未全部通过"))
print("A6 (文案可配置)      : " + ("通过" if results[1][4] else "未通过"))
print("=" * 66)

s.close()
