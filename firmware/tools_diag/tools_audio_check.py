"""音频全面体检:
1) astat   - 当前录音电平/说话判定
2) axtest  - 自环测试(上行采集 + 下行播放 440Hz)
3) 上下行 HTTP 端点实测
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


def cmd(s, c, wait=3.0, show=True, keys=None):
    s.write((c + "\n").encode())
    s.flush()
    t0 = time.time()
    out = []
    while time.time() - t0 < wait:
        try:
            line = s.readline()
            if line:
                t = re.sub(r"\x1b\[[0-9;]*m", "", line.decode("utf-8", "replace")).rstrip()
                out.append(t)
                if show and (keys is None or any(k in t for k in keys)):
                    print("   ", t[-92:])
        except Exception:
            pass
    return out


s = open_clean(3.0)
time.sleep(8.0)

print("=== 1) 录音电平 (astat) ===", flush=True)
cmd(s, "astat", 3.0, keys=("AUDIO", "codec", "codec_dev"))

print("\n=== 2) 连续采样 8 秒电平 (对着麦克风说话应该有跳动) ===", flush=True)
for i in range(8):
    cmd(s, "astat", 1.2, show=False)
    time.sleep(0.9)

print("\n=== 3) 音量 / 静音状态 ===", flush=True)
cmd(s, "vol", 2.0, keys=("音量", "AUDIO"))

print("\n=== 4) 音频自环 (axtest) ===", flush=True)
cmd(s, "axtest", 25.0, show=True, keys=(
    "自环", "AUDIO", "peak", "rms", "码率", "失败", "错误", "ERROR", "解码",
    "播放", "上行", "下行", "非静音"))

s.close()
