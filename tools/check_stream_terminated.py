"""
检查 /stream 是不是被服务端【提前结束】了。

背景:
    firmware/main/main.c 的 stream_handler 里, 等待新帧时用
    httpd_resp_send_chunk(req, NULL, 0) 做"空闲探测", 想靠它感知
    客户端断开。

    但在 ESP-IDF 里, httpd_resp_send_chunk(req, NULL, 0) 的语义是
    【发送 chunked 响应的最后一块】(0\\r\\n\\r\\n), 也就是【结束这个响应】,
    不是"发 0 字节探测"。

    结果: 只要消费者有超过 100ms 没等到新帧, 整个 multipart 流就被
    提前终止 -> 浏览器/客户端看到流结束 -> 断开重连。

这个脚本直接读原始 HTTP 响应, 看是否在数据中途出现 "0\\r\\n\\r\\n"。
"""
import socket
import time

HOST = "192.168.2.138"
PORT = 8080

s = socket.create_connection((HOST, PORT), timeout=10)
s.sendall(b"GET /stream HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % HOST.encode())

data = b""
t0 = time.time()
while time.time() - t0 < 12:
    try:
        b = s.recv(8192)
    except socket.timeout:
        break
    if not b:
        break
    data += b

s.close()

print("总收到: %d bytes" % len(data))

# 头部
head_end = data.find(b"\r\n\r\n")
if head_end > 0:
    print("--- 响应头 ---")
    print(data[:head_end].decode("utf-8", errors="replace"))
    body = data[head_end + 4:]
else:
    body = data

print("--- body 中帧数: %d ---" % body.count(b"--frame"))

# chunked 结束标记
term = body.find(b"\r\n0\r\n\r\n")
print("--- 结束标记 '0\\r\\n\\r\\n' 位置: %s ---" % (term if term >= 0 else "未找到"))

# 结束标记是否出现在最后一帧之前 (即"提前终止")
first_frame_end = body.rfind(b"--frame")
if term >= 0:
    after = body[term + 5:]
    print("--- 结束标记之后还有 %d bytes ---" % len(after))
    if len(after) > 0:
        print("=== 结论: 流被【提前终止】! 之后还残留 %d 字节数据 ===" % len(after))
        print(after[:120])
    else:
        print("=== 结论: 结束标记在数据末尾, 流是【正常结束】 ===")
else:
    print("=== 结论: 12 秒内流没有结束 (仍然在持续) ===")

# 每个 JPEG 的 Content-Length 是否都完整
import re
lens = [int(m) for m in re.findall(rb"Content-Length: (\d+)", body)]
print("--- 声明了 %d 帧, Content-Length 合计 %d 字节 ---" % (len(lens), sum(lens)))
if lens:
    print("--- 实际 body %d 字节, 差值 %d ---" % (len(body), len(body) - sum(lens)))
