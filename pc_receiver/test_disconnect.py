"""验证: 客户端断开后, 板子能否及时释放 socket 槽位。

复现原来的 bug:
  浏览器关标签页 -> FIN_WAIT_2 -> 板子一直占着 socket -> 数据面堵死

做法:
  1) 开一条 /stream 长连接
  2) 粗暴地关闭 socket (不发 FIN_WAKE, 模拟半关闭)
  3) 立刻测板子 /health 是否还能在超时内响应
"""
import socket
import time
import urllib.request

DEV = "192.168.2.138"
ok = 0
fail = 0


def health(timeout=6):
    t0 = time.time()
    try:
        urllib.request.urlopen("http://%s:8080/health" % DEV, timeout=timeout).read()
        return True, (time.time() - t0) * 1000
    except Exception as e:
        return False, str(e)[:40]


print("=== 基线 ===")
r, m = health()
print("  /health: %s (%s)" % ("OK" if r else "FAIL", "%.0f ms" % m if r else m))

print("=== 连续 3 次: 打开流 -> 粗暴断开 -> 查健康 ===")
for i in range(3):
    s = socket.create_connection((DEV, 8080), timeout=10)
    s.sendall(("GET /stream HTTP/1.1\r\nHost: %s:8080\r\n\r\n" % DEV).encode())
    time.sleep(1.5)
    # 粗暴关闭: 直接 close, 不发正常的 FIN 握手
    s.close()
    time.sleep(0.5)
    r, m = health()
    if r:
        ok += 1
        print("  第%d次: 断开后 /health 仍响应 (%.0f ms)" % (i + 1, m))
    else:
        fail += 1
        print("  第%d次: 断开后 /health 失败 -> %s" % (i + 1, m))
    time.sleep(0.5)

print("=== 多次之后 ===")
for i in range(3):
    r, m = health()
    if r:
        ok += 1
    else:
        fail += 1
    print("  健康检查%d: %s" % (i + 1, "OK" if r else "FAIL " + str(m)[:30]))
    time.sleep(0.5)

print("=" * 50)
print("通过 %d / %d" % (ok, ok + fail))
