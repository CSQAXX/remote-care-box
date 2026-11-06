import serial, time, urllib.request, numpy as np, sys, os, re

IP = "192.168.2.138"
port = "COM3"
DEV = r"D:\Software\kanhu\remote-care-box\firmware"
os.chdir(DEV)

W, H = 320, 240

def open_clean(drain_s=2.0):
    s = serial.Serial(port, 115200, timeout=0.2)
    try:
        s.dtr = False; s.rts = False
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < drain_s:
        try: s.readline()
        except Exception: pass
    return s

def cmd(s, c, wait=2.0):
    s.write((c + "\n").encode()); s.flush()
    t0 = time.time()
    while time.time() - t0 < wait:
        try: s.readline()
        except Exception: pass

def http_raw():
    r = urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30)
    return r.read()

def serial_raw(s, path):
    """串口导出一帧原始数据, 头部自带字节数"""
    s.write(b"rawdump\n"); s.flush()
    buf = bytearray()
    while not buf.endswith(b"RAWDUMP "):
        c = s.read(1)
        if not c: return None
        buf += c
    hdr = bytes(buf)
    while not hdr.endswith(b"\n"):
        c = s.read(1)
        if not c: break
        hdr += c
    m = re.search(rb"RAWDUMP (\d+) (\d+)", hdr)
    if not m: return None
    n = int(m.group(2))
    data = bytearray()
    while len(data) < n:
        chunk = s.read(min(65536, n - len(data)))
        if not chunk: break
        data += chunk
    open(path, "wb").write(data)
    return len(data)

def analyze(frames, bytes_per_px, label):
    """frames: list of bytes"""
    print(f"  --- {label} (每像素 {bytes_per_px} 字节) ---")
    arrays = []
    for fb in frames:
        if bytes_per_px == 2:
            arrays.append(np.frombuffer(fb, dtype=">u2").reshape(H, W).astype(np.int32))
        else:
            arrays.append(np.frombuffer(fb, dtype=np.uint8).reshape(H, W).astype(np.int32))
    for i in range(len(arrays) - 1):
        a, b = arrays[i], arrays[i+1]
        d = (a != b)
        ad = np.abs(a - b)
        if bytes_per_px == 2:
            xor = (a ^ b)
            flips = sum(((xor >> k) & 1).sum() for k in range(16)) / 16
        else:
            flips = sum(((a ^ b) >> k & 1).sum() for k in range(8)) / 8
        print(f"    {i}->{i+1}: 像素不同 {100*d.sum()/(W*H):6.2f}%  "
              f"bit翻转 {100*flips/(W*H):5.2f}%  值差>4 {100*(ad>4).sum()/(W*H):6.2f}%")
    # 行平整度: 彩条是横条, 每行内应几乎完全一致
    a = arrays[0]
    flat = sum(1 for y in range(H) if len(np.unique(a[y])) == 1)
    nuniq = [len(np.unique(a[y])) for y in range(H)]
    print(f"    首帧行内取值种类: 最小 {min(nuniq)} 中位 {int(np.median(nuniq))} 最大 {max(nuniq)}")
    print(f"    完全平整的行: {flat}/{H}")
    return arrays

s = open_clean(2.5)
results = {}

for mode, bpp, use_http in ((0, 2, True), (1, 2, True), (2, 1, False)):
    print(f"\n########## mode {mode} ##########", flush=True)
    cmd(s, f"mode {mode}", 6.0)
    cmd(s, "cbar 1", 2.0)
    time.sleep(4.0)   # 等彩条 + AE 稳定
    frames = []
    for i in range(4):
        if use_http:
            data = http_raw()
        else:
            p = f"g_{mode}_{i}.bin"
            n = serial_raw(s, p)
            data = open(p, "rb").read() if n else b""
        frames.append(data)
        time.sleep(0.5)
    ok = [f for f in frames if len(f) == W*H*bpp]
    if len(ok) < 2:
        print(f"  抓帧失败, 长度: {[len(f) for f in frames]}")
        continue
    results[mode] = analyze(ok, bpp, f"mode {mode}")

cmd(s, "cbar 0", 1.5)
s.close()
print("\nALL DONE", flush=True)
