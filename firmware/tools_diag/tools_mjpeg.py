import socket, time, sys

host = "192.168.2.138"
port = 8080
path = "/stream"

s = socket.create_connection((host, port), timeout=10)
s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: curl\r\n\r\n".encode())

buf = bytearray()
t0 = time.time()
while len(buf) < 400000 and time.time() - t0 < 25:
    try:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    except socket.timeout:
        break
s.close()

data = bytes(buf)
print("收到", len(data), "字节")
hdr_end = data.find(b"\r\n\r\n")
print("HTTP 头:", data[:hdr_end][:200])
body = data[hdr_end+4:]
n = body.count(b"\xff\xd8\xff")
print("JPEG 帧数:", n)
if n:
    i = body.find(b"\xff\xd8")
    j = body.find(b"\xff\xd9", i)
    open("stream.jpg", "wb").write(body[i:j+2])
    print("已保存 stream.jpg", j+2-i, "字节")
