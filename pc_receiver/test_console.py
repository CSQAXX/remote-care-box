"""PC 控制台端到端验收测试。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8899"
results = []


def check(name, fn):
    try:
        msg = fn()
        results.append((True, name, msg))
    except Exception as e:
        results.append((False, name, str(e)[:70]))


def t_health():
    d = json.loads(urllib.request.urlopen(BASE + "/api/health", timeout=8).read())
    return "camera_ok=%s audio=%s push=%s heap=%s" % (
        d.get("camera_ok"), d.get("audio_ready"),
        d.get("push_connected"), d.get("free_heap"))


def t_activity():
    d = json.loads(urllib.request.urlopen(BASE + "/api/activity", timeout=8).read())
    return "motion=%s last_active_ts=%s count=%s" % (
        d.get("motion"), d.get("last_active_ts"), d.get("count"))


def t_event():
    d = json.loads(urllib.request.urlopen(BASE + "/api/event", timeout=8).read())
    return "type=%s" % d.get("type")


def t_stream():
    r = urllib.request.urlopen(BASE + "/view/stream", timeout=12)
    t0 = time.time()
    n = 0
    while time.time() - t0 < 4:
        c = r.read(4096)
        if not c:
            break
        n += len(c)
    if n < 50000:
        raise RuntimeError("只收到 %d bytes, 画面不通" % n)
    return "%d bytes / 4s" % n


def t_record():
    r = urllib.request.urlopen(BASE + "/api/record", timeout=20)
    b = r.read()
    if len(b) < 10000:
        raise RuntimeError("只录到 %d bytes" % len(b))
    if b[:4] != b"RIFF":
        raise RuntimeError("不是合法 WAV")
    return "WAV %d bytes (%.1f KB)" % (len(b), len(b) / 1024)


def t_talk():
    """对讲下行: 发 1 秒 440Hz PCM 到板子。"""
    import math
    import struct
    sr = 16000
    pcm = bytearray()
    for i in range(sr):
        v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
        pcm += struct.pack("<h", v)
    req = urllib.request.Request(
        BASE + "/api/talk", data=bytes(pcm), method="POST",
        headers={"Content-Type": "application/octet-stream"})
    d = json.loads(urllib.request.urlopen(req, timeout=25).read())
    if not d.get("ok"):
        raise RuntimeError(str(d))
    return "下发 %d bytes PCM16" % d["bytes"]


def t_notify():
    req = urllib.request.Request(BASE + "/api/notify", data="测试事件".encode(),
                                 method="POST")
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    if not d.get("ok"):
        raise RuntimeError(str(d))
    return "回执成功"


def t_page():
    html = urllib.request.urlopen(BASE + "/", timeout=8).read().decode("utf-8")
    need = ["今天动了没有", "有事没事", "点一下就能看", "按住说话",
            "录一段老人语音", "view/stream", "api/talk", "api/record"]
    missing = [k for k in need if k not in html]
    if missing:
        raise RuntimeError("页面缺要素: %s" % missing)
    return "页面 %d 字节, %d 项要素齐全" % (len(html), len(need))


check("健康检查", t_health)
check("活动检测", t_activity)
check("事件轮询", t_event)
check("画面代理", t_stream)
check("老人语音上行", t_record)
check("对讲下行", t_talk)
check("事件回执", t_notify)
check("控制台页面", t_page)

print("=" * 56)
npass = sum(1 for ok, _, _ in results if ok)
for ok, name, msg in results:
    print("  [%s] %-14s %s" % ("OK" if ok else "FAIL", name, msg))
print("=" * 56)
print("通过 %d / %d" % (npass, len(results)))
