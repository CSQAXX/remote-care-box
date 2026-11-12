# -*- coding: utf-8 -*-
"""校验 diag_turn.py 的 A-law 编码器与固件 g711_ref_table.h 是否逐位一致。"""
import re
import sys

HERE = __file__.rsplit("\\", 1)[0]
src = open(HERE + "\\diag_turn.py", encoding="utf-8").read()
ns = {"__name__": "not_main"}
exec(src.split("def http(")[0], ns)

pairs = re.findall(r"\{\s*(-?\d+),\s*0x([0-9A-Fa-f]{2})\s*\}",
                   open(sys.argv[1], encoding="utf-8", errors="replace").read())
print("参考向量数:", len(pairs))

bad = 0
for pcm, al in pairs:
    got = ns["pcm_to_alaw"]([int(pcm)])[0]
    if got != int(al, 16):
        bad += 1
        if bad <= 5:
            print("MISMATCH pcm=%s expect=0x%s got=0x%02X" % (pcm, al, got))
print("不一致数:", bad)
sys.exit(0 if bad == 0 else 1)
