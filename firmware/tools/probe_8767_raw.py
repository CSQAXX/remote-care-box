# -*- coding: utf-8 -*-
"""复刻板子端那次请求: 原始 socket, HTTP/1.1, Content-Length, 分块写 4096。
目的: 判断卡死到底在电脑这边还是板子这边。"""
import json
import socket
import sys
import time

CFG = r"D:\Software\local-dev\state\iot.json"
HOST = "192.168.2.135"
PORT = 8767
BODY_LEN = 16192

tok = json.load(open(CFG, encoding="utf-8"))["token"]
body = bytes([0xD5]) * BODY_LEN  # A-law 静音附近的值

hdr = (
    "POST /voice HTTP/1.1\r\n"
    "Host: %s:%d\r\n"
    "User-Agent: ESP32 HTTP Client/1.0\r\n"
    "Accept: */*\r\n"
    "X-IoT-Token: %s\r\n"
    "Content-Type: application/octet-stream\r\n"
    "X-Audio-Codec: g711a\r\n"
    "X-Sample-Rate: 16000\r\n"
    "X-Want-Audio: 1\r\n"
    "X-Source: esp32-kanhu\r\n"
    "Content-Length: %d\r\n"
    "\r\n" % (HOST, PORT, tok, BODY_LEN)
).encode()

t0 = time.time()
s = socket.create_connection((HOST, PORT), timeout=20)
print("连上 %.2fs" % (time.time() - t0))
s.sendall(hdr)
sent = 0
while sent < BODY_LEN:
    n = min(4096, BODY_LEN - sent)
    s.sendall(body[sent:sent + n])
    sent += n
print("发完 %d 字节 %.2fs" % (sent, time.time() - t0))

s.settimeout(40)
buf = b""
try:
    while True:
        d = s.recv(8192)
        if not d:
            break
        buf += d
        if b"\r\n\r\n" in buf:
            head, _, rest = buf.partition(b"\r\n\r\n")
            if len(rest) > 4000:
                break
except socket.timeout:
    print("!! 40 秒没等到完整响应, 已收 %d 字节" % len(buf))
s.close()

head = buf.split(b"\r\n\r\n")[0].decode("latin-1", "replace")
print("耗时 %.2fs, 共 %d 字节" % (time.time() - t0, len(buf)))
for line in head.splitlines()[:12]:
    print("  " + line)
