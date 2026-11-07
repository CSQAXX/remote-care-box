import math
import struct
import sys
import urllib.request

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
sr = 16000
pcm = bytearray()
for i in range(sr):                      # 1 秒 440Hz
    v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
    pcm += struct.pack("<h", v)

req = urllib.request.Request(
    "http://192.168.2.138:%d/audio/down" % port,
    data=bytes(pcm), method="POST",
    headers={"Content-Type": "application/octet-stream"})
try:
    with urllib.request.urlopen(req, timeout=40) as r:
        print("状态", r.status)
        print("响应", r.read().decode("utf-8", "replace"))
except Exception as e:
    print("错误:", type(e).__name__, e)
    body = getattr(e, "read", None)
    if body:
        print("响应体:", body().decode("utf-8", "replace"))
