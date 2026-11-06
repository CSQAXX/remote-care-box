#!/usr/bin/env python3
"""
Compare the firmware's G.711 A-law algorithm against the ITU-T reference
(audioop.lin2alaw / audioop.alaw2lin).
"""

import audioop
import struct
import sys
import math

sys.path.insert(0, ".")
from g711_algo import alaw_encode, alaw_decode   # the algorithm under test


def lin2alaw(pcm):
    return audioop.lin2alaw(struct.pack("<h", pcm), 2)[0]


def alaw2lin(byte):
    return struct.unpack("<h", audioop.alaw2lin(bytes([byte]), 2))[0]


print("=== compare firmware algorithm vs ITU reference ===\n")

print("1. encoder")
test_vals = list(range(-2048, 2048)) + list(range(-32768, 32768, 61))
mismatch = []
for v in test_vals:
    got = alaw_encode(v)
    want = lin2alaw(v)
    if got != want:
        mismatch.append((v, want, got))

if mismatch:
    print("   %d mismatches out of %d" % (len(mismatch), len(test_vals)))
    for v, want, got in mismatch[:10]:
        print("   PCM %7d: reference 0x%02X, ours 0x%02X" % (v, want, got))
else:
    print("   [PASS] all %d values match the reference exactly" % len(test_vals))

print("\n2. decoder")
dmis = []
for b in range(256):
    got = alaw_decode(b)
    want = alaw2lin(b)
    if got != want:
        dmis.append((b, want, got))

if dmis:
    print("   %d mismatches out of 256" % len(dmis))
    for b, want, got in dmis[:10]:
        print("   A-law 0x%02X: reference %7d, ours %7d" % (b, want, got))
else:
    print("   [PASS] all 256 byte values match the reference exactly")

print("\n3. round-trip quality (using OUR encoder+decoder)")
worst = 0
for amp in (100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600):
    max_rel = 0
    for i in range(320):
        ph = 2.0 * math.pi * 1000.0 * i / 16000.0
        orig = int(math.sin(ph) * amp)
        orig = max(-32768, min(32767, orig))
        dec = alaw_decode(alaw_encode(orig))
        err = abs(orig - dec)
        max_rel = max(max_rel, err * 100 // amp)
    print("   amp %5d: max rel err %d%%" % (amp, max_rel))
    worst = max(worst, max_rel)
print("   worst relative error: %d%%" % worst)
print("   [%s] rel err <= 15%%" % ("PASS" if worst <= 15 else "FAIL"))
