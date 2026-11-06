import urllib.request, sys, time

base = "http://192.168.1.150"
# 试几个可能的 IP
import socket
def find_ip():
    # 从串口日志里拿 IP 更好, 这里先试常见的
    for ip in ["192.168.1.150", "192.168.1.100", "192.168.1.120", "192.168.1.66"]:
        try:
            s = socket.create_connection((ip, 80), timeout=1.0)
            s.close()
            return ip
        except Exception:
            continue
    return None

ip = find_ip()
print("board ip:", ip)
if ip:
    try:
        r = urllib.request.urlopen(f"http://{ip}/health", timeout=5).read()
        print("health:", r[:400])
    except Exception as e:
        print("health failed:", e)
