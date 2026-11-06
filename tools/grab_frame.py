import socket, sys, re

host, port = "192.168.2.138", 8080
s = socket.create_connection((host, port), timeout=15)
req = ("GET /stream HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\n\r\n" % (host, port)).encode()
s.sendall(req)

buf = b""
while len(buf) < 65536:
    try:
        d = s.recv(4096)
    except socket.timeout:
        break
    if not d:
        break
    buf += d
    if b"\xff\xd9" in buf:
        break
s.close()

i = buf.find(b"\xff\xd8")
j = buf.find(b"\xff\xd9", i)
if i < 0 or j < 0:
    print("no jpeg, head:", buf[:200])
    sys.exit(1)
out = buf[i:j+2]
p = sys.argv[1] if len(sys.argv) > 1 else "frame.jpg"
open(p, "wb").write(out)
print("saved", p, len(out), "bytes")
