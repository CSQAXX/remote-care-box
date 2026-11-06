"""从电脑端验证音频上行流 (和浏览器一样的方式) + 下行播放。"""
import socket, time, re, urllib.request

HOST, PORT = "192.168.2.138", 8080

# ---- G.711 A-law 解码 (标准 ITU-T G.711) ----
def alaw_to_linear(a):
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        return (t + 8) >> 4 if a & 0x80 else -((t + 8) >> 4)
    if seg == 1:
        v = (t + 0x108) >> 4
    else:
        v = (t + 0x108) << (seg - 1)
    return -v if a & 0x80 else v


print("=== 1) GET /audio/level ===")
try:
    r = urllib.request.urlopen(f"http://{HOST}:{PORT}/audio/level", timeout=10)
    print("   ", r.read().decode()[:250])
except Exception as e:
    print("   FAIL:", e)

print("\n=== 2) GET /audio/alaw 抓 3 秒, 解码看电平 ===")
try:
    sk = socket.create_connection((HOST, PORT), timeout=10)
    sk.sendall(f"GET /audio/alaw HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < 3:
        try:
            c = sk.recv(65536)
            if not c:
                break
            buf += c
        except socket.timeout:
            break
    sk.close()
    parts = bytes(buf).split(b"\r\n\r\n", 1)
    body = parts[1] if len(parts) > 1 else b""
    print(f"    3 秒收到 {len(body)} 字节 (设计 16000 B/s -> 期望 ~48000)")

    # 去掉 chunked 编码的头 (每块前有十六进制长度行)
    if b"chunked" in parts[0].lower():
        clean = bytearray()
        i = 0
        while i < len(body):
            nl = body.find(b"\r\n", i)
            if nl < 0:
                break
            try:
                sz = int(body[i:nl].split(b";")[0], 16)
            except ValueError:
                break
            i = nl + 2
            if sz == 0:
                break
            clean += body[i:i + sz]
            i += sz + 2
        body = bytes(clean)
        print(f"    去 chunked 头后 {len(body)} 字节")

    pcm = [alaw_to_linear(b) for b in body]
    if pcm:
        peak = max(abs(v) for v in pcm)
        rms = int((sum(v * v for v in pcm) / len(pcm)) ** 0.5)
        nz = sum(1 for v in pcm if abs(v) > 50)
        print(f"    解码后 peak={peak} rms={rms}")
        print(f"    超过 50 的样本: {nz}/{len(pcm)} ({100.0*nz/len(pcm):.1f}%)")
        print(f"    -> 有效信号占比 {'正常 (麦克风在拾音)' if nz > len(pcm)*0.02 else '偏低, 可能环境安静'}")
except Exception as e:
    print("   FAIL:", e)

print("\n=== 3) POST /audio/down (模拟客户端下行) ===")
try:
    import math
    pcm = bytearray()
    for i in range(16000):
        v = int(math.sin(2 * math.pi * 440 * i / 16000) * 8000)
        pcm += v.to_bytes(2, "little", signed=True)
    req = urllib.request.Request(f"http://{HOST}:{PORT}/audio/down", data=bytes(pcm),
                                 method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    r = urllib.request.urlopen(req, timeout=20)
    print("   ", r.status, r.read().decode()[:200])
except Exception as e:
    print("   FAIL:", e)

print("\n=== 4) 再测一次 level (下行期间上行应该被静音) ===")
try:
    r = urllib.request.urlopen(f"http://{HOST}:{PORT}/audio/level", timeout=10)
    print("   ", r.read().decode()[:250])
except Exception as e:
    print("   FAIL:", e)
