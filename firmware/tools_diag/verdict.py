import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'

def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    i = d.find(b'\r\n\r\n')
    return np.frombuffer(d[i+4:i+4+153600], dtype=np.uint8).reshape(240, 320, 2)

f = load('raw3_1.bin')
A = f[:,:,0].astype(np.int64)
B = f[:,:,1].astype(np.int64)
R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31

# The image content itself: reconstruct a "correct" 6-bit green estimate
# using the fact that in a natural image R,G,B track each other.
# We know R5 and B5 are clean and R5~B5 corr 0.949.
# G3 (green hi 3 bits) is clean; if the image's green follows R5/B5,
# then the MISSING green lo-3-bits should also be smooth & track R5.
# => we can test: does G2 look like it SHOULD be smooth? (i.e. is it garbage)
print('=== RECONSTRUCTION TEST ===')
print('If G2 were legitimate green-low-bits it would correlate with the')
print('other channels just like G3 does. Measured:')
print('  corr(G3, R5) = %+.3f   corr(G3, B5) = %+.3f' % (np.corrcoef(G3.ravel(),R5.ravel())[0,1], np.corrcoef(G3.ravel(),B5.ravel())[0,1]))
print('  corr(G2, R5) = %+.3f   corr(G2, B5) = %+.3f' % (np.corrcoef(G2.ravel(),R5.ravel())[0,1], np.corrcoef(G2.ravel(),B5.ravel())[0,1]))
print('  => G3 behaves like a real channel; G2 does NOT.')
print()
print('  G3 hist:', np.bincount(G3.ravel(), minlength=8)[:8])
print('  G2 hist:', np.bincount(G2.ravel(), minlength=8)[:8], '  <-- ~uniform = random')
print()
print('=== WHAT THE USER SEES ===')
print('Green low 3 bits carry 3/64 of the green value, but because the')
print('green channel error is +/-3.5/64 of full scale and the eye is most')
print('sensitive to green, this shows up as heavy chroma noise / flower-screen.')
print()
print('=== IS THE ERROR DETERMINISTIC (converter bug) or RANDOM (DMA)? ===')
for fn in ('raw3_1.bin', 'raw3_2.bin', 'raw_0.bin', 'raw_4.bin', 'raw2_0.bin'):
    g = load(fn)
    g2 = (g[:,:,1] >> 5).astype(np.int64)
    r5 = (g[:,:,0] >> 3).astype(np.int64)
    # per-frame: how well does G2 match the CORRELATION it would need?
    hist = np.bincount(g2.ravel(), minlength=8)[:8] / g2.size
    ent = -float((hist * np.log2(hist + 1e-12)).sum())
    print('  %-13s corr(G2,R5)=%+.3f  corr(G2,G3)=%+.3f  G2 entropy %.2f bits' % (
        fn, np.corrcoef(g2.ravel(), r5.ravel())[0,1],
        np.corrcoef(g2.ravel(), (g[:,:,0]&7).ravel())[0,1], ent))
