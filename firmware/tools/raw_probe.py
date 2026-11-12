# -*- coding: utf-8 -*-
"""原始 TCP 探针 (多线程版): 每个连接独立线程, 互不阻塞。

用法: python tools/raw_probe.py [端口]
"""
import socket
import struct
import sys
import threading
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799


def handle(c, addr):
    t0 = time.time()
    print("\n==== 来自 %s:%d 的连接 ====" % addr, flush=True)
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = c.recv(4096)
        if not d:
            print("!! 头没收全就断了, 已收 %d" % len(buf), flush=True)
            return
        buf += d
        print("  +%d (累计 %d)" % (len(d), len(buf)), flush=True)
    head, _, body = buf.partition(b"\r\n\r\n")
    print("---- 请求头 ----\n%s\n---- 头结束 %.3fs ----"
          % (head.decode("latin-1", "replace"), time.time() - t0), flush=True)
    n = 0
    for line in head.decode("latin-1", "replace").splitlines():
        if line.lower().startswith("content-length:"):
            n = int(line.split(":", 1)[1].strip())
    got = len(body)
    print("声明正文 %d, 头里带来 %d" % (n, len(body)), flush=True)
    last = t0
    while got < n:
        d = c.recv(8192)
        if not d:
            print("!! 正文没收齐就断: %d/%d" % (got, n), flush=True)
            break
        got += len(d)
        now = time.time()
        print("  +%d -> %d/%d (+%.3fs)" % (len(d), got, n, now - last), flush=True)
        last = now
    print("== 正文 %d/%d, 用时 %.3fs ==" % (got, n, time.time() - t0), flush=True)
    pcm = b"".join(struct.pack("<h", 3000 if (i // 40) % 2 else -3000) for i in range(16000))
    r = ("HTTP/1.0 200 OK\r\nContent-Type: application/octet-stream\r\n"
         "Content-Length: %d\r\nX-User-Text: probe\r\nX-Reply-Text: probe-ok\r\n\r\n"
         % len(pcm)).encode()
    try:
        c.sendall(r + pcm)
        print("已回 200 (%d 字节音频)" % len(pcm), flush=True)
    except Exception as exc:
        print("!! 回包失败: %s" % exc, flush=True)
    time.sleep(1)
    c.close()


srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", PORT))
srv.listen(8)
print("监听 %d (多线程) ..." % PORT, flush=True)
while True:
    c, addr = srv.accept()
    threading.Thread(target=handle, args=(c, addr), daemon=True).start()
