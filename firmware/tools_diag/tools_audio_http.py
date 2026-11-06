"""从电脑端直接测音频 HTTP 端点, 并查设备堆内存。"""
import socket, time, re, urllib.request, json

HOST, PORT = "192.168.2.138", 8080


def ser_open(drain_s=3.0):
    import serial
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


def ser_cmd(s, c, wait=2.0):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                out.append(re.sub(r"\x1b\[[0-9;]*m", "",
                                  line.decode("utf-8", "replace")).rstrip())
        except Exception:
            pass
    return out


print("=== 1) GET /audio/level ===")
try:
    r = urllib.request.urlopen(f"http://{HOST}:{PORT}/audio/level", timeout=10)
    print("   ", r.status, r.read().decode()[:300])
except Exception as e:
    print("   FAIL:", e)

print("\n=== 2) GET /audio/alaw (抓 5 秒, 看有没有字节流) ===")
try:
    sk = socket.create_connection((HOST, PORT), timeout=10)
    sk.sendall(f"GET /audio/alaw HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < 5:
        try:
            c = sk.recv(65536)
            if not c:
                break
            buf += c
        except socket.timeout:
            break
    sk.close()
    head = bytes(buf).split(b"\r\n\r\n", 1)
    print("    响应头:")
    for line in head[0].decode("latin1").split("\r\n"):
        print("      ", line)
    body = head[1] if len(head) > 1 else b""
    print(f"    5 秒收到 {len(body)} 字节 (期望约 8000 = 16000 B/s * 0.5)")
    if body:
        # A-law 静音是 0xD5 (或 0x55 变体), 统计分布
        from collections import Counter
        c = Counter(body)
        print(f"    最常见的 5 个字节值: {c.most_common(5)}")
        print(f"    不同字节值个数: {len(c)}")
except Exception as e:
    print("   FAIL:", e)

print("\n=== 3) 设备堆内存 ===")
s = ser_open(3.0)
time.sleep(2.0)
for l in ser_cmd(s, "mem", 3.0):
    if any(k in l for k in ("heap", "Heap", "HEAP", "free", "Free", "最小")):
        print("   ", l[-90:])
for l in ser_cmd(s, "free", 3.0):
    if any(k in l for k in ("heap", "Heap", "HEAP", "free", "Free", "最小", "剩余")):
        print("   ", l[-90:])
s.close()
