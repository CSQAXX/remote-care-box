import urllib.request, hashlib, time

IP = "192.168.2.138"

def get_raw(capture=False):
    url = f"http://{IP}:8080/raw" + ("?capture=1" if capture else "")
    return urllib.request.urlopen(url, timeout=30).read()

print("=== 传输完整性测试: 同一个缓冲区连续下载两次 (不触发新抓帧) ===")
for trial in range(4):
    a = get_raw(False)
    b = get_raw(False)
    same = (a == b)
    diff = 0 if same else sum(1 for x, y in zip(a, b) if x != y)
    print(f"  第{trial}次: len={len(a)}/{len(b)}  完全一致={same}  不同字节={diff}")

print()
print("=== 抓帧一致性: 抓 8 帧, 看第几帧开始稳定 ===")
frames = []
for i in range(8):
    d = get_raw(True)
    frames.append(d)
    print(f"  帧{i}: len={len(d)} md5={hashlib.md5(d).hexdigest()[:16]}")

print()
print("=== 相邻帧比较 ===")
for i in range(len(frames) - 1):
    a, b = frames[i], frames[i+1]
    same = a == b
    diff = sum(1 for x, y in zip(a, b) if x != y)
    print(f"  {i}->{i+1}: 完全一致={same}  不同字节={diff} ({100*diff/len(a):.2f}%)")
