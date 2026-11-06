"""验证 LCD 显示通路: 用 lcd1 (固定图样) / lcd2 (合成渐变) 对比摄像头模式。

- lcd1/lcd2 完全不碰摄像头数据 -> 如果屏幕上稳定, 说明 SPI/DMA 通路 OK。
- 再看 LCD 直绘的帧数增长和耗时, 确认没有卡死。
"""
import serial, time, re

PORT = "COM3"


def open_clean(drain_s=3.0):
    s = serial.Serial(PORT, 115200, timeout=0.2)
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


def cmd(s, c, wait=2.0, show=True):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                t = re.sub(r"\x1b\[[0-9;]*m", "", line.decode("utf-8", "replace")).rstrip()
                if show and ("LCD" in t or "模式" in t or "显示" in t):
                    print("   ", t[-84:])
        except Exception:
            pass


def collect(s, secs=10):
    """收集 LCD 直绘统计"""
    lines = []
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            line = s.readline()
            if line and b"LCD" in line:
                lines.append(re.sub(r"\x1b\[[0-9;]*m", "",
                                    line.decode("utf-8", "replace")).strip()[-80:])
        except Exception:
            pass
    return lines


s = open_clean(3.0)
time.sleep(8.0)

for mode, name in (("lcd1", "固定测试图样"), ("lcd2", "合成渐变图"), ("lcd0", "摄像头画面")):
    print(f"=== {mode} ({name}) ===", flush=True)
    cmd(s, mode, 2.0)
    time.sleep(3.0)
    ls = collect(s, 10)
    for l in ls[-4:]:
        print("   ", l)
    print()
s.close()
