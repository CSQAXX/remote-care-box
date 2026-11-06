import numpy as np, glob, hashlib

def load():
    out = []
    for f in sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw3_*.bin')):
        d = open(f, 'rb').read()
        i = d.find(b'\r\n\r\n')
        body = d[i+4:]
        print("%s: body=%d  sha=%s" % (f.split('\\')[-1], len(body),
              hashlib.sha1(body[:153600]).hexdigest()[:12]))
        if len(body) < 153600:
            continue
        out.append(body[:153600])
    return out

raw = load()
print("usable frames:", len(raw))
if len(raw) < 2:
    raise SystemExit("need 2 frames")

F = [np.frombuffer(r, dtype=np.uint8) for r in raw]

def corr(a, b):
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    a = a - a.mean(); b = b - b.mean()
    da = np.sqrt((a*a).sum()); db = np.sqrt((b*b).sum())
    if da == 0 or db == 0:
        return 0.0
    return float((a*b).sum() / (da*db))

f0, f1 = F[0], F[1]
W = 320
A0 = f0[0::2].astype(np.int64); B0 = f0[1::2].astype(np.int64)
A1 = f1[0::2].astype(np.int64); B1 = f1[1::2].astype(np.int64)
print("A0 len", len(A0), "B0 len", len(B0))

print("\n=== FIELD-LEVEL (16-bit pixel = A<<8 | B) ===")
fields = {
    "R5 = A>>3 ": (A0>>3, A1>>3),
    "G3 = A&7  ": (A0&7,  A1&7),
    "G2 = B>>5 ": (B0>>5, B1>>5),
    "B5 = B&31 ": (B0&31, B1&31),
}
for k, (p, q) in fields.items():
    print("  %s temporal corr = %+.4f   std(f0) = %6.3f" % (k, corr(p, q), np.asarray(p).std()))

print("\n=== BIT-LEVEL (16 bits of the pixel, MSB first) ===")
bits0 = np.unpackbits(f0); bits1 = np.unpackbits(f1)
names = {0:'R4',1:'R3',2:'R2',3:'R1',4:'R0',
         5:'G2h',6:'G1h',7:'G0h',
         8:'G2l',9:'G1l',10:'G0l',
         11:'B4',12:'B3',13:'B2',14:'B1',15:'B0'}
for bit in range(16):
    print("  bit %2d (%s): temporal corr = %+.4f" % (bit, names[bit], corr(bits0[bit::16], bits1[bit::16])))

print("\n=== SANITY: field corr must be <= max component bit corr ===")
r5bits = [corr(bits0[b::16], bits1[b::16]) for b in range(5)]
print("  R5 field = %+.4f ; its bits = %s ; max = %+.4f" % (corr(A0>>3, A1>>3),
      ["%+.3f" % x for x in r5bits], max(r5bits)))
b5bits = [corr(bits0[b::16], bits1[b::16]) for b in range(11,16)]
print("  B5 field = %+.4f ; its bits = %s ; max = %+.4f" % (corr(B0&31, B1&31),
      ["%+.3f" % x for x in b5bits], max(b5bits)))

print("\n=== is frame1 actually a DIFFERENT frame? ===")
print("  corr(A0, A1) = %+.4f   corr(B0, B1) = %+.4f" % (corr(A0, A1), corr(B0, B1)))
print("  fraction of identical bytes A: %.4f  B: %.4f" % (
      (A0 == A1).mean(), (B0 == B1).mean()))
