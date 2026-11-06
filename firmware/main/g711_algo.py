"""
G.711 A-law algorithm (Python port of the firmware's g711.c).

Kept in sync with firmware/main/g711.c line by line.
Used by the validation scripts to check the quantization logic
against Python's audioop (the ITU-T reference implementation).
"""

SEG_AEND = [0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF]


def g711_search(val, table):
    for i, t in enumerate(table):
        if val <= t:
            return i
    return len(table)


def alaw_encode(sample):
    pcm = sample >> 3
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


def to_signed(v):
    return v - 0x10000 if v >= 0x8000 else v


def alaw_decode(byte):
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
    # 注意: 不再 <<3 —— 解码输出直接就是 16bit 采样值
    return to_signed(v & 0xFFFF)
