# -*- coding: utf-8 -*-
"""MJPEG 流原始字节抓取 (绕过 http.client 的分块解码)

用途: 定位 /stream 为什么只返回 148 字节。
直接用 socket 读原始字节, 能看到服务端到底发了什么。
"""
import socket
import sys
import time

HOST = "192.168.2.138"
PORT = 8080


def main():
    s = socket.create_connection((HOST, PORT), timeout=8)
    s.sendall(b"GET /stream HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\n"
               b"Accept: */*\r\nConnection: close\r\n\r\n")
    total = 0
    frames = 0
    t0 = time.time()
    buf = b""
    while time.time() - t0 < 8:
        try:
            c = s.recv(4096)
        except socket.timeout:
            break
        if not c:
            print("  [服务端关闭连接]")
            break
        total += len(c)
        buf += c
        frames += c.count(b"--frame")
        if len(buf) > 600:
            break
    s.close()

    print(f"总字节: {total}, --frame 出现次数: {frames}")
    print("--- 前 500 字节原始内容 ---")
    print(repr(buf[:500]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
