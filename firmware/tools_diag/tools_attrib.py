"""
间歇性损坏的归因实验。

关键事实: 静态彩条下, 连续帧有时【逐位相同】, 有时 60% 不同。
=> 损坏是【间歇性】的, 不是传感器的确定性错误。
=> 典型的总线争用 (PSRAM) 症状: 谁在抢总线, 谁就是元凶。

逐项关闭可疑负载, 看间歇性损坏是否消失:
  A 基线          : 全部开
  B LCD 不画摄像头 : lcd1 (固定图样)
  C LCD 慢速      : spiclk 10000000
  D WiFi 关       : wifioff (此时只能用串口抓帧)
  E LCD + 慢 SPI
"""
import numpy as np
import urllib.request, time, serial, os, re

IP = "192.168.2.138"
W, H = 320, 240
os.chdir(r"D:\Software\kanhu\remote-care-box\firmware")


def open_clean(drain_s=2.0):
    s = serial.Serial("COM3", 115200, timeout=0.2)
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
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def serial_raw(s):
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
    return bytes(data)


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)


def bitflip(a, b):
    xor = a ^ b
    return float(sum(((xor >> k) & 1).sum() for k in range(16))) / 16 / (W * H)


def measure(s, label, n=10, use_serial=False):
    frames = []
    for i in range(n):
        try:
            d = serial_raw(s) if use_serial else http_raw()
            if d and len(d) == W * H * 2:
                frames.append(load(d))
        except Exception as e:
            print(f"    抓帧失败: {e}")
        time.sleep(0.3)
    if len(frames) < 3:
        print(f"  {label:20s} 抓帧不足 ({len(frames)})")
        return None
    fl = [bitflip(frames[i], frames[i + 1]) for i in range(len(frames) - 1)]
    clean = sum(1 for f in fl if f < 0.005)
    print(f"  {label:20s} 逐帧bit翻转: {[f'{f*100:.1f}' for f in fl]}")
    print(f"  {'':20s}   -> 完全干净的帧对 {clean}/{len(fl)}  平均 {np.mean(fl)*100:.1f}%")
    return dict(label=label, fl=fl, clean=clean, total=len(fl))


def main():
    s = open_clean(2.5)
    cmd(s, "mode 0", 5.0)
    cmd(s, "pda 1", 6.0)
    cmd(s, "pclk 1", 2.0)
    cmd(s, "cbar 1", 2.0)
    time.sleep(12.0)   # 让 AE 完全稳定 (彩条也走 AE)

    out = []
    print("=== A 基线 (全部开) ===", flush=True)
    out.append(measure(s, "A baseline"))

    print("=== B LCD 不画摄像头 (lcd1) ===", flush=True)
    cmd(s, "lcd1", 1.0)
    time.sleep(2.0)
    out.append(measure(s, "B lcd=pattern"))

    print("=== C LCD 慢速 SPI 10MHz ===", flush=True)
    cmd(s, "lcd0", 1.0)
    cmd(s, "spiclk 10000000", 5.0)
    time.sleep(2.0)
    out.append(measure(s, "C spi=10M"))

    print("=== D LCD 固定图样 + 慢 SPI ===", flush=True)
    cmd(s, "lcd1", 1.0)
    time.sleep(2.0)
    out.append(measure(s, "D pattern+10M"))

    print("=== E 恢复 LCD 摄像头 + 快 SPI ===", flush=True)
    cmd(s, "lcd0", 1.0)
    cmd(s, "spiclk 60000000", 5.0)
    time.sleep(2.0)
    out.append(measure(s, "E camera+60M"))

    print("=== F 关 WiFi (串口抓帧) ===", flush=True)
    cmd(s, "wifioff", 3.0)
    time.sleep(2.0)
    out.append(measure(s, "F wifioff", n=8, use_serial=True))
    cmd(s, "wifix", 2.0)
    time.sleep(6.0)

    cmd(s, "cbar 0", 1.0)
    s.close()
    print()
    print("=== 汇总 ===")
    for o in out:
        if o:
            print(f"  {o['label']:20s} 干净帧对 {o['clean']}/{o['total']}  平均翻转 {np.mean(o['fl'])*100:5.1f}%")
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
