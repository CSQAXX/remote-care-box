import serial, time, sys, os

PORT = "COM3"
BAUD = 115200
OUT = sys.argv[1] if len(sys.argv) > 1 else "boot_cold.log"
DUR = int(sys.argv[2]) if len(sys.argv) > 2 else 45

s = serial.Serial(PORT, BAUD, timeout=0.2)
start = time.time()
buf = []
print(f"[*] 打开 {PORT} @ {BAUD}, 采集 {DUR}s -> {OUT}")
while time.time() - start < DUR:
    try:
        raw = s.read(4096)
        if raw:
            txt = raw.decode("utf-8", "replace")
            buf.append(txt)
            sys.stdout.write(txt)
            sys.stdout.flush()
    except Exception as e:
        print(f"[!] {e}")
s.close()
with open(OUT, "w", encoding="utf-8") as f:
    f.write("".join(buf))
print(f"\n[*] 已保存 {OUT} ({sum(len(b) for b in buf)} bytes)")
