"""最终验收: 验证 lcd1/lcd2 不再与直绘冲突, 且摄像头模式完全正常。"""
import serial, time, re, urllib.request, json, socket

HOST = "192.168.2.138"


def health():
    try:
        r = urllib.request.urlopen(f"http://{HOST}:8080/health", timeout=6)
        return json.loads(r.read().decode())
    except Exception as e:
        return {"err": str(e)}


def open_clean(drain_s=3.0):
    s = serial.Serial("COM3", 115200, timeout=0.2)
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


def cmd(s, c, wait=5.0):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            l = s.readline()
            if l:
                out.append(re.sub(r"\x1b\[[0-9;]*m", "",
                                  l.decode("utf-8", "replace")).rstrip())
        except Exception:
            pass
    return out


def stream_fps(seconds=4.0):
    sk = socket.create_connection((HOST, 8080), timeout=10)
    sk.sendall(f"GET /stream HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            c = sk.recv(65536)
            if not c:
                break
            buf += c
        except socket.timeout:
            break
    sk.close()
    return bytes(buf).count(b"\xff\xd8\xff") / seconds


s = open_clean(3.0)
time.sleep(12.0)

print("=" * 62)
print("最终验收报告  2026-11-03")
print("=" * 62)

# ---- 1) 模式互斥测试 ----
print("\n[1] 显示模式互斥 (本次 bug 的核心)")
for mode, label in (("lcd2", "合成渐变图"), ("lcd1", "固定测试图样")):
    cmd(s, mode, 2.0)
    time.sleep(3)
    d0 = health().get("lcd_frames")
    time.sleep(4)
    d1 = health().get("lcd_frames")
    inc = (d1 - d0) if (d0 is not None and d1 is not None) else -1
    ok = "OK" if inc == 0 else "FAIL"
    print(f"    {mode} ({label}): 直绘帧增量 = {inc:3d}  [{ok}] "
          f"应为 0 (诊断模式下不能同时画摄像头画面)")

cmd(s, "lcd0", 2.0)
time.sleep(3)
d0 = health().get("lcd_frames")
time.sleep(4)
d1 = health().get("lcd_frames")
inc = (d1 - d0) if (d0 is not None and d1 is not None) else -1
ok = "OK" if inc >= 15 else "FAIL"
print(f"    lcd0 (摄像头画面): 直绘帧增量 = {inc:3d}  [{ok}]  应 ~20 (5fps)")

# ---- 2) 花屏量化 ----
print("\n[2] 花屏量化指标 g2_bad")
for l in cmd(s, "q", 6.0):
    m = re.search(r"g2_bad=[\d.]+%.*", l)
    if m:
        print(f"    {m.group(0)}")

# ---- 3) 帧缓冲稳定性 ----
print("\n[3] 帧缓冲稳定性 (DMA 有没有偷偷改写我们的帧)")
for l in cmd(s, "hash", 8.0):
    if "HASH" in l and "第二次" in l:
        print(f"    {l[l.find('HASH'):]}")

# ---- 4) 帧率 ----
print("\n[4] 帧率")
h = health()
print(f"    摄像头生产: {h.get('cam_frames')} 帧   "
      f"直绘耗时: {h.get('lcd_draw_us')}us   撕裂: {h.get('torn_frames')}")
try:
    fps = stream_fps(4.0)
    print(f"    MJPEG 远程流: {fps:.1f} fps")
except Exception as e:
    print(f"    MJPEG 远程流: FAIL {e}")

# ---- 5) 内存与音频 ----
print("\n[5] 内存 / 音频")
h = health()
print(f"    PSRAM: {h.get('psram')}   音频就绪: {h.get('audio_ready')}   "
      f"采样率: {h.get('audio_rate')}")
for l in cmd(s, "mem", 3.0):
    if "剩余" in l:
        print(f"    {l[l.find('内部'):][:70]}")

# ---- 6) 错误扫描 ----
print("\n[6] 错误扫描 (6 秒)")
t0 = time.time()
seen = {}
while time.time() - t0 < 6:
    try:
        l = s.readline()
        if l:
            t = re.sub(r"\x1b\[[0-9;]*m", "", l.decode("utf-8", "replace")).rstrip()
            if any(k in t for k in ("E (", "W (", "panic", "溢出", "重启")):
                k = t[12:72]
                seen[k] = seen.get(k, 0) + 1
    except Exception:
        pass
if seen:
    for k, v in sorted(seen.items(), key=lambda x: -x[1])[:12]:
        print(f"    x{v}  {k}")
else:
    print("    (无错误)")

print("\n" + "=" * 62)
s.close()
