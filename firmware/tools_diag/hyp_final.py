import numpy as np

OUT = r'D:\Software\kanhu\remote-care-box'

def load(fn):
    d = open(OUT + '\\' + fn, 'rb').read()
    i = d.find(b'\r\n\r\n')
    return np.frombuffer(d[i+4:i+4+153600], dtype=np.uint8).reshape(240, 320, 2)

f = load('raw3_1.bin')
A = f[:,:,0].astype(np.int64)   # even byte of each 16-bit word
B = f[:,:,1].astype(np.int64)   # odd byte

print('HYPOTHESIS P: buffer = RGB565 from converter (G2 garbage)')
print('HYPOTHESIS Q: buffer = raw YUYV 8-bit (converter bypassed)')
print()
print('Under Q, the byte stream would be Y0 U0 Y1 V1 Y2 U2 Y3 V3')
print('  -> even bytes = luma (all pixels), odd bytes = chroma (U,V alternating)')
print('  -> chroma is 2x-subsampled => adjacent same-component chroma samples')
print('     are 2 REAL pixels apart => should be comparable to |Y[x]-Y[x+2]|')
print()
even2 = np.abs(A[:,2:] - A[:,:-2]).mean()
odd2  = np.abs(B[:,2:] - B[:,:-2]).mean()   # same component, 2 real px apart
even1 = np.abs(A[:,1:] - A[:,:-1]).mean()
odd1  = np.abs(B[:,1:] - B[:,:-1]).mean()   # U vs V = different components
print('  luma    : |x-x+1| %6.2f   |x-x+2| %6.2f' % (even1, even2))
print('  chroma  : |x-x+1| %6.2f   |x-x+2| %6.2f   <-- same-component' % (odd1, odd2))
print('  ratio (chroma|x-x+2| / luma|x-x+2|) = %.2f' % (odd2 / even2))
print('  -> chroma should be MUCH smoother than luma in a natural scene.')
print('     A ratio >> 1 means the odd bytes are NOT chroma => HYPOTHESIS Q DEAD.')
print()

print('=== cross-check with HYPOTHESIS P (RGB565): inverse BT601 -> chroma pair-share ===')
R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
G = (G3 << 3) | G2
R = R5 << 3; Bc = B5 << 3
y  = 0.299*R + 0.587*G + 0.114*Bc
uu = -0.168736*R - 0.331264*G + 0.5*Bc + 128
vv = 0.5*R - 0.418688*G - 0.081312*Bc + 128
dU = np.abs(uu[:,0::2] - uu[:,1::2]).mean()
dV = np.abs(vv[:,0::2] - vv[:,1::2]).mean()
print('  |U[2k]-U[2k+1]| = %.3f   |V[2k]-V[2k+1]| = %.3f' % (dU, dV))
print('  (chroma shared per pixel-pair => ~0 if this really is converted RGB565)')
print()
print('  chroma range U %.1f..%.1f  V %.1f..%.1f' % (uu.min(), uu.max(), vv.min(), vv.max()))
print()

print('=== GREEN RECONSTRUCTION FEASIBILITY ===')
print('If we replace G2 by the median of its neighbours (clamped to G3 bin),')
print('how much does the image change vs. how much noise does it remove?')
for fn in ('raw3_1.bin', 'raw3_2.bin'):
    g = load(fn)
    AA = g[:,:,0].astype(np.int64); BB = g[:,:,1].astype(np.int64)
    gg3 = (AA & 7); gg2 = (BB >> 5)
    left  = np.roll(gg2, 1, axis=1); right = np.roll(gg2, -1, axis=1)
    left[:, 0] = right[:, 0]; right[:, -1] = left[:, -1]
    med = (left + right) // 2
    lo = gg3 * 8; hi = gg3 * 8 + 7
    med = np.clip(med, lo, hi)
    fixed = med & 7
    def ac(P):
        p = P.astype(float).ravel(); return np.corrcoef(p[:-1], p[1:])[0,1]
    print('  %s' % fn)
    print('     before: G2 autocorr %.3f   mean %.3f' % (ac(gg2), gg2.mean()))
    print('     after : G2 autocorr %.3f   mean %.3f' % (ac(fixed), fixed.mean()))
    # how different are the repaired greens from originals?
    print('     changed %.1f%% of pixels' % ((fixed != gg2).mean()*100))
