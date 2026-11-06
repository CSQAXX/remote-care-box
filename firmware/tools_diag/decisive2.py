import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'

def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    i = d.find(b'\r\n\r\n')
    return np.frombuffer(d[i+4:i+4+153600], dtype=np.uint8).reshape(240, 320, 2)

def sm(P):
    C = P[1:-1, 1:-1]
    nb = np.stack([P[1:-1,:-2], P[1:-1,2:], P[:-2,1:-1], P[2:,1:-1]])
    return float(np.abs(C - np.median(nb, axis=0)).mean())

f = load('raw3_1.bin')
A = f[:,:,0].astype(np.int64)
B = f[:,:,1].astype(np.int64)

print('=== TEST 1: is the frame buffer raw YUV422 (converter NOT applied)? ===')
# Then even byte = Y, odd byte = chroma (U,V alternating)
U = B[:, 0::2].astype(float)
V = B[:, 1::2].astype(float)
# chroma in real YUV422 is shared by pixel pair => U[k] pairs with V[k]
print('  U adjacent-pair smoothness  |U[k]-U[k+1]| %.2f' % float(np.abs(U[:,1:]-U[:,:-1]).mean()))
print('  V adjacent-pair smoothness  |V[k]-V[k+1]| %.2f' % float(np.abs(V[:,1:]-V[:,:-1]).mean()))
print('  Y smoothness                |Y[x]-Y[x+1]| %.2f' % float(np.abs(A[:,1:]-A[:,:-1]).mean()))

print()
print('=== TEST 2: inverse BT601 from observed RGB565 -> is chroma pair-shared? ===')
R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
G = (G3 << 3) | G2
R = (R5 << 3) | R5
Bl = (B5 << 3) | B5
print('  R,G,B ranges: R %d..%d  G %d..%d  B %d..%d' % (R.min(),R.max(),G.min(),G.max(),Bl.min(),Bl.max()))
# inverse BT601 (full range)
y  = 0.299*R + 0.587*G + 0.114*Bl
uu = -0.168736*R - 0.331264*G + 0.5*Bl + 128
vv = 0.5*R - 0.418688*G - 0.081312*Bl + 128
# chroma shared per pair => u[2k]==u[2k+1]
dU = np.abs(uu[:,0::2] - uu[:,1::2]).mean()
dV = np.abs(vv[:,0::2] - vv[:,1::2]).mean()
print('  |U[2k]-U[2k+1]| mean %.3f   (0 => pair-shared chroma => REAL YUV422 source)' % dU)
print('  |V[2k]-V[2k+1]| mean %.3f' % dV)
# compare against a random-noise baseline
print('  baseline |random diff| for U range ~ %.1f' % (uu.max()-uu.min()))

print()
print('=== TEST 3: exhaustive 16-bit word rotation search ===')
words = (A << 8) | B            # big endian word
def fieldscore(w):
    hi = (w >> 8) & 0xFF; lo = w & 0xFF
    fs = {'R5': hi>>3, 'G3': hi&7, 'G2': lo>>5, 'B5': lo&31}
    return max(sm(v) for v in fs.values())
best = []
for s in range(16):
    wr = ((words << s) | (words >> (16 - s))) & 0xFFFF
    best.append((fieldscore(wr), 'rot%02d' % s))
# also byte-swapped variants
words_le = (B << 8) | A
for s in range(16):
    wr = ((words_le << s) | (words_le >> (16 - s))) & 0xFFFF
    best.append((fieldscore(wr), 'LE rot%02d' % s))
best.sort()
for v, n in best[:8]:
    print('   %-10s worst_field_smoothness %.3f' % (n, v))

print()
print('=== TEST 4: where do the 3 random bits of the odd byte come from? ===')
top3 = B >> 5
for name, cand in (('A>>5', A >> 5), ('A&7', A & 7), ('B&31', B & 31),
                   ('prev B>>5', np.roll(B,1,axis=1) >> 5),
                   ('next B>>5', np.roll(B,-1,axis=1) >> 5),
                   ('prev A>>5', np.roll(A,1,axis=1) >> 5)):
    if cand.shape != top3.shape: continue
    eq = (cand == top3).mean()
    cc = np.corrcoef(cand.ravel().astype(float), top3.ravel().astype(float))[0,1]
    print('   top3 == %-10s  exact %.3f  corr %.3f' % (name, eq, cc))
