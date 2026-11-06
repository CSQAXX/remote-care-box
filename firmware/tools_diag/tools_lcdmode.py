import serial, time, collections, sys

PORT = "COM3"


def read(dur):
    s = serial.Serial(PORT, 115200, timeout=0.2)
    s.reset_input_buffer()
    t0 = time.time()
    buf = b""
    while time.time() - t0 < dur:
        try:
            d = s.read(4096)
            if d:
                buf += d
        except Exception:
            pass
    s.close()
    return buf.decode("utf-8", "replace")


def send(cmd, wait=1.0):
    s = serial.Serial(PORT, 115200, timeout=0.3)
    s.reset_input_buffer()
    s.write((cmd + "\n").encode())
    s.flush()
    time.sleep(wait)
    s.close()


def report(label, dur=14):
    txt = read(dur)
    c = collections.Counter()
    for line in txt.splitlines():
        if "测试图样已绘制" in line:
            c["pattern"] += 1
        if "LCD 直绘" in line:
            c["camdraw"] += 1
        if "合成图" in line:
            c["synth"] += 1
        if "LCD 模式" in line:
            print("  >>", line.strip()[-90:])
    print(f"  {label}: {dict(c)}")


print("=== 发送 lcd0 (强制回到摄像头模式) ===")
send("lcd0", 1.5)
report("lcd0 后 14 秒", 14)
