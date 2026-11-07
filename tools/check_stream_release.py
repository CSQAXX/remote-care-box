"""
验证 /stream 客户端断开后, 板子能否【快速释放】流配额。

背景:
    STREAM_MAX_CLIENTS = 2 (数据面 8080 的推流并发上限)。
    客户端粗暴关闭后, 如果板子侧不能及时发现, 配额就被占死,
    后面再连就 503 / 连接超时。

做法:
    1) 连一条 /stream
    2) 粗暴 close()
    3) 立刻再连一条, 看能不能连上 (连不上 = 配额没释放)
"""
import socket
import time

DEV = "192.168.2.138"


def try_stream(tag):
    try:
        s = socket.create_connection((DEV, 8080), timeout=6)
        s.sendall(("GET /stream HTTP/1.1\r\nHost: %s:8080\r\n\r\n" % DEV).encode())
        time.sleep(0.8)
        return s, "OK"
    except Exception as e:
        return None, "FAIL(%s)" % str(e)[:40]


print("=== 基线: 连一条流 ===")
s, m = try_stream("base")
print("  第一次连接: %s" % m)

if s:
    print("=== 粗暴断开 ===")
    s.close()
    t0 = time.time()

    print("=== 断开后连续尝试重连 (看多久能恢复) ===")
    freed_ms = None
    for i in range(10):
        time.sleep(0.3)
        s2, m2 = try_stream("retry%d" % i)
        if s2:
            if freed_ms is None:
                freed_ms = (time.time() - t0) * 1000
                print("  断开后 %.0f ms 恢复可连" % freed_ms)
            s2.close()
        else:
            print("  +%.0f ms 仍连不上: %s" % ((time.time() - t0) * 1000, m2))

    print("=== 控制面 8081 是否始终可用 ===")
    import urllib.request
    for i in range(3):
        t = time.time()
        try:
            urllib.request.urlopen("http://%s:8081/health" % DEV, timeout=5).read()
            print("  控制面 OK (%.0f ms)" % ((time.time() - t) * 1000))
        except Exception as e:
            print("  控制面 FAIL: %s" % str(e)[:50])
        time.sleep(0.4)
