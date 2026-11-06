import numpy as np, glob
from PIL import Image

def load(pat):
    out = []
    for f in sorted(glob.glob(pat)):
        d = open(f, 'rb').read()
        i = d.find(b'\r\n\r\n')
        body = d[i + 4:]
        if len(body) < 153600:
            continue
        out.append(np.frombuffer(body[:153600], dtype=np.uint8).reshape(240, 320, 2))
    return np.array(out)

fr = load(r'D:\Software\kanhu\remote-care-box\raw3_*.bin')
b0 = fr[0]
HI = b0[:, :, 0].astype(np.int64)
LO = b0[:, :, 1].astype(np.int64)

def adj(P):
    return float(np.abs(P[:, 1:] - P[:, :-1]).mean())

print("=== field smoothness (current alignment) ===")
print("  R5 = HI>>3      adj %.2f" % adj(HI >> 3))
print("  G3 = HI&7       adj %.2f" % adj(HI & 7))
print("  G2 = LO>>5      adj %.2f" % adj(LO >> 5))
print("  B5 = LO&0x1F    adj %.2f" % adj(LO & 0x1F))
print("\n  -> 只有 LO 的高 3 位 (绿色位) 是噪声源")

print("\n=== SEARCH: shift ONLY the LO plane, does green become smooth? ===")
G3 = HI & 7
for s in range(-6, 7):
    LOs = np.roll(LO, s, axis=1)
    g2 = LOs >> 5
    # combined green = G3<<3 | G2
    G = ((G3 << 3) | g2) & 0x3F
    print("  shift %+d: G2 adj %6.2f   G(combined) adj %6.2f   B5 adj %5.2f"
          % (s, adj(g2), adj(G), adj(LOs & 0x1F)))

print("\n=== SEARCH: shift ONLY the HI plane ===")
for s in range(-6, 7):
    HIs = np.roll(HI, s, axis=1)
    G3s = HIs & 7
    G = ((G3s << 3) | (LO >> 5)) & 0x3F
    print("  shift %+d: G3 adj %6.2f   G(combined) adj %6.2f   R5 adj %5.2f"
          % (s, adj(G3s), adj(G), adj(HIs >> 3)))

print("\n=== INTERLEAVE hypotheses: maybe bytes alternate differently ===")
print("Current: HI=even bytes, LO=odd bytes")
# Hypothesis: pixels are (LO,HI) little-endian
G = (((LO & 7) << 3) | (HI >> 5)) & 0x3F
print("  swapped words: G adj %.2f  R adj %.2f  B adj %.2f" % (
    adj(G), adj((LO & 0xF8)), adj(HI & 0x1F)))
