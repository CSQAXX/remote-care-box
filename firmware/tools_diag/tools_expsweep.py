"""
【决定性实验】手动曝光/增益到底生效没有?

如果 exp 200 -> exp 1200 时画面亮度【不变】,
说明我们写 AAAA_EN/EXPOSURE 的动作被传感器忽略了 (或者 AEC 实际还在工作),
所以"亮暗交替"就是传感器自己在自动调节。

如果亮度【跟着变】, 说明手动模式生效了, 那亮暗交替另有原因。
"""
import serial, time
import numpy as np
import urllib.request

W, H = 320, 240
IP = "192.168.2.138"
PORT = "COM3"


def send(cmd, wait=1.2):
    s = serial.Serial(PORT, 115200, timeout=0.3)
    s.reset_input_buffer()
    s.write((cmd + "\n").encode())
    s.flush()
    time.sleep(wait)
    s.close()


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int64)


def measure(n=6, label=""):
    lum = []
    for i in range(n):
        lum.append(load(raw()).mean())
        time.sleep(0.2)
    s = " ".join(f"{l:6.0f}" for l in lum)
    print(f"  {label:22s} 亮度: {s}   中位={np.median(lum):7.0f}")
    return float(np.median(lum))


print("=== 基线 (手动 exp=480 gain=20) ===")
base = measure(label="baseline")

print()
print("=== 曝光扫描 (gain 固定 20) ===")
for e in (120, 240, 480, 960, 1200):
    send(f"exp {e}")
    measure(label=f"exp={e}")

print()
print("=== 增益扫描 (exp 固定 480) ===")
send("exp 480")
for g in (0, 10, 20, 40, 63):
    send(f"gain {g}")
    measure(label=f"gain={g}")

print()
print("=== 恢复默认 ===")
send("gain 20")
send("exp 480")

print()
print("=== 再测一次默认, 看是否仍在交替 ===")
measure(n=10, label="final")
