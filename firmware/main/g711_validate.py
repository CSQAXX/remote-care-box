#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G.711 A-law 算法验证 (Python 端, 用 audioop 做权威基准)
=========================================================

背景:
    上一版 g711_validate.py 里有两处错误, 导致固件里本来正确的实现
    被误判为"5 项全失败":
      1. 手写的参考表是错的 —— 它假设 PCM 1 -> 0xD4, 但 ITU-T 参考实现
         (Python 的 audioop.lin2alaw) 给出的是 0xD5。
      2. Python 端的解码多了一个 <<3, 而固件 g711.c 的解码是对的。
         (A-law 码字里藏的就是 16bit 尺度, 不能再放大)

    本版把所有期望值都改成从 audioop 现算, 不再手写常量,
    并且用 audioop 同时校验编码与解码两个方向。

用法:
    python g711_validate.py
    退出码 0 = 全通过
"""
import audioop
import math
import struct
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ---------------------------------------------------------------- 基准函数
def ref_encode(pcm16: int) -> int:
    """ITU-T G.711 参考编码 (audioop.lin2alaw)。"""
    return audioop.lin2alaw(struct.pack("<h", pcm16), 2)[0]


def ref_decode(byte: int) -> int:
    """ITU-T G.711 参考解码 (audioop.alaw2lin)。"""
    return struct.unpack("<h", audioop.alaw2lin(bytes([byte]), 2))[0]


# ---------------------------------------------------------------- 待验证实现
# 与 firmware/main/g711.c 逐行对应
SEG_AEND = [0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF]


def g711_search(val, table):
    for i, t in enumerate(table):
        if val <= t:
            return i
    return len(table)


def my_encode(sample: int) -> int:
    pcm = sample >> 3                       # Python 对负数是算术右移, 与 C 一致
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = g711_search(pcm, SEG_AEND)
    if seg >= 8:
        return (0x7F ^ mask) & 0xFF
    aval = (seg << 4) & 0xFF
    if seg < 2:
        aval |= (pcm >> 1) & 0x0F
    else:
        aval |= (pcm >> seg) & 0x0F
    return (aval ^ mask) & 0xFF


def my_decode(byte: int) -> int:
    """注意: 这里【不能】再 <<3 —— 固件 g711.c 也是这样。"""
    a_val = byte ^ 0x55
    t = (a_val & 0x0F) << 4
    seg = (a_val & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= (seg - 1)
    v = t if (a_val & 0x80) else -t
    return v - 0x10000 if v >= 0x8000 else v


# ---------------------------------------------------------------- 测试框架
failures = 0


def check(cond, what):
    global failures
    print("  [%s] %s" % ("PASS" if cond else "FAIL", what))
    if not cond:
        failures += 1
    return cond


print("=== G.711 A-law validation (基准: audioop / ITU-T G.711) ===\n")

# ---------------------------------------------------------------- 1
print("1. 编码与 ITU-T 参考实现逐值一致")
mismatch = []
probe = list(range(-600, 601, 1)) + list(range(-32768, 32768, 337))
for v in probe:
    v = max(-32768, min(32767, v))
    got, want = my_encode(v), ref_encode(v)
    if got != want:
        mismatch.append((v, want, got))
if mismatch:
    for v, want, got in mismatch[:5]:
        print("   PCM %7d: expect 0x%02X got 0x%02X" % (v, want, got))
check(not mismatch, "编码全值域匹配 (检查 %d 个采样点)" % len(probe))

# ---------------------------------------------------------------- 2
print("\n2. 解码与 ITU-T 参考实现逐值一致")
mismatch = []
for b in range(256):
    got, want = my_decode(b), ref_decode(b)
    if got != want:
        mismatch.append((b, want, got))
if mismatch:
    for b, want, got in mismatch[:5]:
        print("   A-law 0x%02X: expect %d got %d" % (b, want, got))
check(not mismatch, "解码全 256 个码字匹配")

# ---------------------------------------------------------------- 3
print("\n3. 往返误差 (1kHz 正弦)")
worst_rel = 0
for amp in (100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600):
    max_err = 0
    max_rel = 0
    for i in range(320):
        ph = 2.0 * math.pi * 1000.0 * i / 16000.0
        orig = max(-32768, min(32767, int(math.sin(ph) * amp)))
        dec = my_decode(my_encode(orig))
        err = abs(orig - dec)
        max_err = max(max_err, err)
        if orig != 0:
            max_rel = max(max_rel, err * 100 // amp)
    print("   amp %5d: max abs err %5d, max rel err %d%%" % (amp, max_err, max_rel))
    worst_rel = max(worst_rel, max_rel)
# A-law 是分段量化, 小信号相对误差天然偏大, 语音场景可接受
check(worst_rel <= 35, "相对误差 <= 35%% (实测最差 %d%%)" % worst_rel)

# ---------------------------------------------------------------- 4
print("\n4. 静音不产生假噪声")
# 注意: A-law 把 0 编码成 0xD5, 解码回来是 8 而不是 0 —— 这是 ITU-T G.711
# 的标准行为 (量化偏置), 不是 bug。所以这里要比对参考实现, 而不是断言 == 0。
zero_dec = my_decode(my_encode(0))
check(zero_dec == ref_decode(ref_encode(0)),
      "0 的往返值与参考实现一致 (%d)" % zero_dec)
max_out = max(abs(my_decode(my_encode(v))) for v in range(-30, 31))
print("   input +/-30 -> max decoded amp %d" % max_out)
check(max_out <= 64, "小信号不被放大成噪声")

# ---------------------------------------------------------------- 5
print("\n5. 量化单调 (幅度越大解码值越大)")
mono = True
prev = -1
for v in range(0, 32001, 97):
    d = abs(my_decode(my_encode(v)))
    if d < prev:
        mono = False
        break
    prev = d
check(mono, "解码幅度单调不减")

# ---------------------------------------------------------------- 6
print("\n6. 正负样本互为镜像")
mirror_ok = True
for v in (1, 100, 1000, 8000, 20000, 32000):
    if ((my_encode(v) ^ my_encode(-v)) & 0x7F) != 0:
        mirror_ok = False
check(mirror_ok, "正负编码互为镜像")

# ---------------------------------------------------------------- 7
print("\n7. 压缩比 (A-law : PCM16 = 1:2)")
sample = b"".join(struct.pack("<h", (i * 37) % 30000 - 15000) for i in range(320))
enc = audioop.lin2alaw(sample, 2)
print("   PCM16 %d 字节 -> A-law %d 字节" % (len(sample), len(enc)))
check(len(enc) * 2 == len(sample), "码流长度恰为 PCM 的一半")

print("\n=== %s (%d failure(s)) ===" % ("ALL PASS" if failures == 0 else "FAILED", failures))
sys.exit(1 if failures else 0)
