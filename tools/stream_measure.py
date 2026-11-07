"""
拉流的同时采样 /health, 确认生产端与发送端的真实速率。

为什么不用 urllib:
    urllib 遇到 chunked 响应的结束标记 "0\\r\\n\\r\\n" 会停止读取,
    于是测出来的速率远低于真实值 (之前误判成"拉流把帧率拖到 0.33fps")。

用法:
    python tools/stream_measure.py 12
"""
import json
import socket
import threading
import time
import urllib.request

HOST = "192.168.2.138"
SECS = float(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 12.0

stop = threading.Event()
samples = []


def sampler():
    while not stop.is_set():
        try:
            h = json.load(urllib.request.urlopen(
                "http://%s:8081/health" % HOST, timeout=3))
            samples.append((time.time(), h["cam_frames"], h["jpg_frames"],
                            h["frames"], h["clients"]))
        except Exception:
            pass
        time.sleep(1.0)


t = threading.Thread(target=sampler, daemon=True)
t.start()

time.sleep(2)
base = samples[-1]

s = socket.create_connection((HOST, 8080), timeout=10)
s.sendall(b"GET /stream HTTP/1.1\r\nHost: %s\r\n\r\n" % HOST.encode())

n = 0
frames = 0
t0 = time.time()
while time.time() - t0 < SECS:
    try:
        b = s.recv(16384)
    except socket.timeout:
        break
    if not b:
        break
    n += len(b)
    frames += b.count(b"--frame")
s.close()

time.sleep(1.2)
stop.set()
t.join(timeout=2)

end = samples[-1]
dt = end[0] - base[0]

print("=== 拉流 %ds ===" % SECS)
print("  网络: %d bytes, %d 帧 (%.1f fps)" % (n, frames, frames / SECS))
print("  cam_frames +%d (%.1f fps)" % (end[1] - base[1], (end[1] - base[1]) / dt))
print("  jpg_frames +%d (%.1f fps)" % (end[2] - base[2], (end[2] - base[2]) / dt))
print("  发出的 frames +%d" % (end[3] - base[3]))
print("  clients = %d" % end[4])
