import serial, time, re, urllib.request, sys

port = "COM3"

def open_clean(drain_s=3.0):
    s = serial.Serial(port, 115200, timeout=0.2)
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

def cmd(s, c, wait=2.0):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                txt = line.decode("utf-8", "replace").rstrip()
                out.append(txt)
                if "PROD" in txt or "LCD" in txt or "cam_frames" in txt:
                    print("   ", txt[-100:])
        except Exception:
            pass
    return out

s = open_clean(3.0)
print("=== 稳定态帧率 (10 秒) ===", flush=True)
cmd(s, "q", 2.0)
time.sleep(8)
cmd(s, "stat", 2.0)
print("\n=== 打开浏览器流 ===", flush=True)
cmd(s, "q", 1.0)
s.close()

# 抓 MJPEG
import socket
url = "http://192.168.2.138:8080/stream"
try:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    r = urllib.request.urlopen(req, timeout=15)
    data = r.read(200000)
    print("  流首字节:", data[:20])
    # 找 JPEG 帧
    n = data.count(b"\xff\xd8\xff")
    print(f"  收到 {len(data)} 字节, JPEG 帧数 {n}")
    open("stream.jpg", "wb").write(data[data.find(b"\xff\xd8"):data.rfind(b"\xff\xd9")+2])
    print("  已保存 stream.jpg")
except Exception as e:
    print("  流抓取失败:", e)
