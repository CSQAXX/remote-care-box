import serial, time, urllib.request, numpy as np

IP = "192.168.2.138"
port = "COM3"

def open_clean(drain_s=2.0):
    s = serial.Serial(port, 115200, timeout=0.2)
    try:
        s.dtr = False
        s.rts = False
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < drain_s:
        try:
            s.readline()
        except Exception:
            pass
    return s

def cmd(s, c, wait=2.0):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            s.readline()
        except Exception:
            pass

def grab(tag):
    r = urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30)
    d = r.read()
    open(tag, "wb").write(d)
    return len(d)

W, H = 320, 240

def arr(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

s = open_clean(2.5)
print("cbar 1")
cmd(s, "cbar 1", 2.0)
time.sleep(4.0)          # 等彩条稳定
for i in range(4):
    n = grab(f"cbhttp{i}.bin")
    print(f"  cbhttp{i}: {n}")
    time.sleep(0.5)
cmd(s, "cbar 0", 1.5)
s.close()

print()
print("=== 彩条帧间对比 (静止图样, 差异应接近 0) ===")
fs = [arr(f"cbhttp{i}.bin") for i in range(4)]
for i in range(len(fs) - 1):
    a, b = fs[i], fs[i+1]
    d = (a != b)
    xor = (a ^ b)
    flips = sum(((xor >> k) & 1).sum() for k in range(16))
    ad = np.abs(a - b)
    print(f"  {i} vs {i+1}: 差异 {100*d.sum()/(W*H):6.2f}%  bit翻转 {100*flips/16/(W*H):5.2f}%  "
          f"差>4 {100*(ad>4).sum()/(W*H):6.2f}%  差>100 {100*(ad>100).sum()/(W*H):6.2f}%")

print()
print("=== 彩条行平整度 (横条: 每行内应完全一致) ===")
for i, a in enumerate(fs):
    flat = sum(1 for y in range(H) if len(np.unique(a[y])) == 1)
    print(f"  帧{i}: 完全平整的行 {flat}/{H} ({100*flat/H:.1f}%)")
