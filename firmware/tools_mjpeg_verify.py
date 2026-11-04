# -*- coding: utf-8 -*-
"""MJPEG 流端到端验证

用途:
  1) 抓 /stream 的原始字节, 解出 JPEG, 存成 .jpg
  2) 用 PIL 打开, 确认能正常解码出 320x240 画面
  3) 统计帧率和平均帧大小 (对比 A4 要求的 >= 10fps)

用法:
  python tools_mjpeg_verify.py            # 抓 6 秒
  python tools_mjpeg_verify.py --seconds 10
"""
import argparse
import os
import socket
import sys
import time

HOST = "192.168.2.138"
PORT = 8080
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "evidence")


def grab(seconds):
    """抓 MJPEG 原始字节, 返回 (原始数据, 帧数)"""
    s = socket.create_connection((HOST, PORT), timeout=8)
    s.sendall(
        f"GET /stream HTTP/1.1\r\nHost: {HOST}\r\nAccept: */*\r\n\r\n".encode()
    )
    buf = b""
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            c = s.recv(8192)
        except socket.timeout:
            break
        if not c:
            break
        buf += c
    s.close()
    return buf


def split_jpegs(raw):
    """从原始字节里切出所有完整的 JPEG (FFD8 ... FFD9)"""
    frames = []
    i = 0
    while True:
        start = raw.find(b"\xff\xd8\xff", i)
        if start < 0:
            break
        end = raw.find(b"\xff\xd9", start)
        if end < 0:
            break
        frames.append(raw[start:end + 2])
        i = end + 2
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=6)
    ap.add_argument("--save", default="mjpeg_frame.jpg")
    args = ap.parse_args()

    raw = grab(args.seconds)
    frames = split_jpegs(raw)

    print(f"抓取 {args.seconds} 秒: 原始 {len(raw)} 字节, 解出 {len(frames)} 帧 JPEG")
    if not frames:
        print("!! 没有解出任何 JPEG")
        print(repr(raw[:400]))
        return 1

    sizes = [len(f) for f in frames]
    print(f"帧大小: 最小 {min(sizes)} 最大 {max(sizes)} 平均 {sum(sizes)//len(sizes)} 字节")
    print(f"估算帧率: {len(frames)/args.seconds:.1f} fps  (A4 要求 >= 10fps)")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.normpath(os.path.join(OUT_DIR, args.save))
    with open(out, "wb") as fp:
        fp.write(frames[0])
    print(f"已保存首帧: {out}")

    try:
        from PIL import Image
        im = Image.open(out)
        im.load()
        print(f"PIL 解码成功: {im.size} {im.mode}")
        px = im.convert("RGB").getdata()
        uniq = len(set(list(px)[::37]))
        print(f"抽样颜色数: {uniq} (>10 说明不是纯色/黑屏)")
    except ImportError:
        print("(没装 PIL, 跳过解码检查)")
    except Exception as e:
        print(f"PIL 解码失败: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
