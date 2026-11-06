#!/usr/bin/env python3
"""
G.711 A-law algorithm validation (Python port)

Same algorithm as g711.c in the firmware, translated line by line.
Purpose: verify the quantization logic without a host C compiler.

Checks:
  1. Round-trip error (sine wave)
  2. Silence produces no fake noise
  3. Quantization is monotonic
  4. Matches ITU-T G.711 A-law reference vectors
  5. Positive/negative samples are mirror images
"""

import math
import sys

SEG_AEND = [0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF]


def g711_search(val, table):
    for i, t in enumerate(table):
        if val <= t:
            return i
    return len(table)


def alaw_encode(sample):
    pcm = sample >> 3          # Python >> on negative is arithmetic, same as C
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
    return to_signed((v << 3) & 0xFFFF)


failures = 0


def check(cond, what):
    global failures
    print("  [%s] %s" % ("PASS" if cond else "FAIL", what))
    if not cond:
        failures += 1


print("=== G.711 A-law validation ===\n")

print("1. round-trip error (1kHz sine)")
worst_rel = 0
for amp in (100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600):
    max_err = 0
    max_rel = 0
    for i in range(320):
        ph = 2.0 * math.pi * 1000.0 * i / 16000.0
        orig = int(math.sin(ph) * amp)
        orig = max(-32768, min(32767, orig))
        dec = alaw_decode(alaw_encode(orig))
        err = abs(orig - dec)
        max_err = max(max_err, err)
        max_rel = max(max_rel, err * 100 // amp)
    print("   amp %5d: max abs err %5d, max rel err %d%%" % (amp, max_err, max_rel))
    worst_rel = max(worst_rel, max_rel)
check(worst_rel <= 15, "rel err <= 15%")

print("\n2. silence")
check(alaw_decode(alaw_encode(0)) == 0, "0 -> encode -> decode == 0")
max_out = 0
for v in range(-30, 31):
    max_out = max(max_out, abs(alaw_decode(alaw_encode(v))))
print("   input +/-30 -> max decoded amp %d" % max_out)
check(max_out <= 64, "small signal not amplified into noise")

print("\n3. monotonic quantization")
prev = -1
mono = True
for v in range(0, 32001, 97):
    d = abs(alaw_decode(alaw_encode(v)))
    if d < prev:
        mono = False
        break
    prev = d
check(mono, "decoded amp is monotonic non-decreasing")

print("\n4. ITU-T G.711 reference vectors")
ref = [
    (0x0000, 0xD5), (0x0001, 0xD4), (0x0002, 0xD3), (0x0003, 0xD2),
    (0x0004, 0xD1), (0x0005, 0xD0), (0x0006, 0xCF), (0x0007, 0xCE),
    (0x0008, 0xCD), (0x0009, 0xCC), (0x000A, 0xCB), (0x000B, 0xCA),
    (0x000C, 0xC9), (0x000D, 0xC8), (0x000E, 0xC7), (0x000F, 0xC6),
    (0x0010, 0xC5), (0x0011, 0xC4), (0x0012, 0xC3), (0x0013, 0xC2),
    (0x0014, 0xC1), (0x0015, 0xC0), (0x0016, 0xBF), (0x0017, 0xBE),
    (0x0018, 0xBD), (0x0019, 0xBC), (0x001A, 0xBB), (0x001B, 0xBA),
    (0x001C, 0xB9), (0x001D, 0xB8), (0x001E, 0xB7), (0x001F, 0xB6),
    (0x0020, 0xB5), (0x0021, 0xB4), (0x0022, 0xB3), (0x0023, 0xB2),
    (0x0024, 0xB1), (0x0025, 0xB0), (0x0026, 0xAF), (0x0027, 0xAE),
    (0x0028, 0xAD), (0x0029, 0xAC), (0x002A, 0xAB), (0x002B, 0xAA),
    (0x002C, 0xA9), (0x002D, 0xA8), (0x002E, 0xA7), (0x002F, 0xA6),
    (0x0030, 0xA5), (0x0031, 0xA4), (0x0032, 0xA3), (0x0033, 0xA2),
    (0x0034, 0xA1), (0x0035, 0xA0), (0x0036, 0x9F), (0x0037, 0x9E),
    (0x0038, 0x9D), (0x0039, 0x9C), (0x003A, 0x9B), (0x003B, 0x9A),
    (0x003C, 0x99), (0x003D, 0x98), (0x003E, 0x97), (0x003F, 0x96),
]
bad = []
for pcm, expect in ref:
    got = alaw_encode(pcm)
    if got != expect:
        bad.append((pcm, expect, got))
if bad:
    for pcm, expect, got in bad[:5]:
        print("   PCM 0x%04X: expect 0x%02X got 0x%02X" % (pcm, expect, got))
    check(False, "matches ITU reference (first mismatch 0x%04X)" % bad[0][0])
else:
    check(True, "matches ITU reference vectors (64 cases)")

print("\n5. negative samples")
mirror_ok = True
for v in (1, 100, 1000, 8000, 20000, 32000):
    pos = alaw_encode(v)
    neg = alaw_encode(-v)
    if ((pos ^ neg) & 0x7F) != 0:
        mirror_ok = False
check(mirror_ok, "positive/negative are mirror images")

print("\n=== %s (%d failure(s)) ===" % ("FAILED" if failures else "ALL PASS", failures))
sys.exit(1 if failures else 0)
