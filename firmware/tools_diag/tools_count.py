import serial, sys, time, re, collections

port = "COM3"
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
s = serial.Serial(port, 115200, timeout=0.2)
s.reset_input_buffer()
t0 = time.time()
buf = b""
cnt = collections.Counter()
while time.time() - t0 < dur:
    try:
        d = s.read(4096)
        if d:
            buf += d
    except Exception:
        pass
s.close()
txt = buf.decode("utf-8", "replace")
for line in txt.splitlines():
    if "测试图样已绘制" in line or "图样" in line:
        cnt["test_pattern_draws"] += 1
    if "LCD 直绘" in line:
        cnt["camera_direct_draws"] += 1
    if "LCD 模式" in line:
        cnt["mode_change"] += 1
        print("MODE:", line)
print(cnt)
