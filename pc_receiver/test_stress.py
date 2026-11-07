"""压力场景: 画面占满数据面时, 控制面功能是否仍然可用。

这是本次修复的核心目标 —— 数据面/控制面分离。
"""
import json
import threading
import time
import urllib.request

DEV = "http://192.168.2.138"
results = []


def rec(name, ok, msg):
    results.append((ok, name, msg))


def hold_stream(dur):
    """占住一路长连接 (数据面)。"""
    try:
        r = urllib.request.urlopen(DEV + ":8080/stream", timeout=20)
        t0 = time.time()
        while time.time() - t0 < dur:
            if not r.read(4096):
                break
        return True
    except Exception:
        return False


def probe_health():
    ok = 0
    fail = 0
    lat = []
    for _ in range(10):
        t0 = time.time()
        try:
            urllib.request.urlopen(DEV + ":8081/health", timeout=6).read()
            ok += 1
            lat.append((time.time() - t0) * 1000)
        except Exception:
            fail += 1
        time.sleep(0.3)
    return ok, fail, (min(lat), max(lat)) if lat else (0, 0)


def probe_talk():
    import math
    import struct
    sr = 16000
    pcm = bytearray()
    for i in range(sr // 2):
        v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
        pcm += struct.pack("<h", v)
    req = urllib.request.Request(
        DEV + ":8081/audio/down", data=bytes(pcm), method="POST",
        headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        return bool(d.get("ok")) and d.get("played_frames", 0) > 0
    except Exception as e:
        return "ERR " + str(e)[:50]


def probe_record():
    try:
        src = urllib.request.urlopen(DEV + ":8081/audio/alaw", timeout=20)
        alaw = b""
        deadline = time.time() + 2.5
        while time.time() < deadline:
            c = src.read(4096)
            if not c:
                break
            alaw += c
        src.close()
        return len(alaw)
    except Exception as e:
        return "ERR " + str(e)[:50]


print("=== 场景: 3 路画面占满数据面 (8080) ===")
streams = [threading.Thread(target=hold_stream, args=(18,)) for _ in range(3)]
for t in streams:
    t.start()
time.sleep(3)

ok, fail, (mn, mx) = probe_health()
rec("控制面健康检查", fail == 0,
    "成功 %d / 失败 %d, 延迟 %d-%d ms" % (ok, fail, mn, mx))

r = probe_talk()
rec("控制面对讲下行", r is True, str(r))

n = probe_record()
rec("控制面老人语音", isinstance(n, int) and n > 10000, "%s bytes" % n)

for t in streams:
    t.join()

time.sleep(2)
ok2, fail2, _ = probe_health()
rec("画面断开后恢复", fail2 == 0, "成功 %d / 失败 %d" % (ok2, fail2))

print("=" * 56)
for ok, name, msg in results:
    print("  [%s] %-18s %s" % ("OK" if ok else "FAIL", name, msg))
npass = sum(1 for o, _, _ in results if o)
print("=" * 56)
print("通过 %d / %d" % (npass, len(results)))
