#!/usr/bin/env python3
"""
Generate ground-truth G.711 A-law reference vectors using Python's audioop.

audioop.lin2alaw() is the ITU-T G.711 A-law reference implementation.
We use it to produce a C test table, so the firmware's own encoder can be
checked against a known-correct implementation.

Output: g711_ref_table.h  (a C array of {pcm16, alaw_byte} pairs)
"""

import audioop
import struct
import collections


def lin2alaw(pcm):
    """PCM16 -> A-law byte, via the ITU-T reference implementation in audioop"""
    return audioop.lin2alaw(struct.pack("<h", pcm), 2)[0]


def alaw2lin(byte):
    """A-law byte -> PCM16, via the ITU-T reference implementation in audioop"""
    return struct.unpack("<h", audioop.alaw2lin(bytes([byte]), 2))[0]


pairs = []

# Full sweep of small values (exercises the first two segments)
for v in range(0, 256):
    pairs.append((v, lin2alaw(v)))
    pairs.append((-v, lin2alaw(-v)))

# Sparse sweep across the whole range (exercises all 8 segments)
for v in range(256, 32768, 257):
    pairs.append((v, lin2alaw(v)))
    pairs.append((-v, lin2alaw(-v)))

# Boundary values around each segment edge
for v in (0, 1, 2, 3, 7, 8, 15, 16, 31, 32, 63, 64, 127, 128, 255, 256,
          511, 512, 1023, 1024, 2047, 2048, 4095, 4096,
          8191, 8192, 16383, 16384, 32767, -32768):
    pairs.append((v, lin2alaw(v)))

print("generated %d reference pairs" % len(pairs))

with open("g711_ref_table.h", "w") as f:
    f.write("/*\n")
    f.write(" * G.711 A-law 参考向量 (自动生成, 不要手改)\n")
    f.write(" *\n")
    f.write(" * 生成方式: python g711_gen_ref.py  (用 audioop.lin2alaw, 即 ITU-T\n")
    f.write(" * G.711 官方参考实现产生)\n")
    f.write(" *\n")
    f.write(" * 用途: 固件里的 g711_alaw_encode() 必须和这张表完全一致,\n")
    f.write(" * 否则说明量化逻辑写错了 —— 语音会变调或者全是噪声。\n")
    f.write(" */\n\n")
    f.write("#ifndef G711_REF_TABLE_H\n#define G711_REF_TABLE_H\n\n")
    f.write("#include <stdint.h>\n\n")
    f.write("typedef struct {\n    int16_t pcm;\n    uint8_t alaw;\n} g711_ref_pair_t;\n\n")
    f.write("static const g711_ref_pair_t s_g711_ref[] = {\n")
    for pcm, alaw in pairs:
        f.write("    { %6d, 0x%02X },\n" % (pcm, alaw))
    f.write("};\n\n")
    f.write("#define G711_REF_COUNT (sizeof(s_g711_ref) / sizeof(s_g711_ref[0]))\n\n")
    f.write("#endif /* G711_REF_TABLE_H */\n")

print("wrote g711_ref_table.h")

segs = collections.Counter()
for pcm, alaw in pairs:
    segs[(alaw & 0x70) >> 4] += 1
print("segment distribution:", dict(sorted(segs.items())))

# Show a few examples so we can eyeball the mapping
print("\nsample mappings:")
for pcm, alaw in pairs[:5] + pairs[100:105] + pairs[-5:]:
    print("   PCM %7d -> A-law 0x%02X" % (pcm, alaw))
