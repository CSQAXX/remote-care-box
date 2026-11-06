import numpy as np
import glob, os

OUT = r'D:\Software\kanhu\remote-care-box'


def load(path):
    d = open(path, 'rb').read()
    return np.frombuffer(d[:153600], dtype=np.uint8).reshape(240, 320, 2)


def stats(f, name):
    A = f[:, :, 0].astype(np.int64)
    B = f[:, :, 1].astype(np.int64)
    R5 = A >> 3; G3 = A & 7; G2 = B >> 5; B5 = B & 31
    out = {}
    out['corr(G2,R5)'] = np.corrcoef(G2.ravel(), R5.ravel())[0, 1]
    out['corr(G2,B5)'] = np.corrcoef(G2.ravel(), B5.ravel())[0, 1]
    out['corr(G2,G3)'] = np.corrcoef(G2.ravel(), G3.ravel())[0, 1]
    # G3 vs B5 (both legit channels) for reference
    out['corr(G3,B5)'] = np.corrcoef(G3.ravel(), B5.ravel())[0, 1]
    out['corr(R5,B5)'] = np.corrcoef(R5.ravel(), B5.ravel())[0, 1]
    # entropy of G2
    h = np.bincount(G2.ravel(), minlength=8) / G2.size
    out['G2ent'] = float(-(h * np.log2(h + 1e-12)).sum())
    print('%-22s ' % name + '  '.join('%s=%+.3f' % (k, v) for k, v in out.items()))
    return out


print('=== SCENE-INDEPENDENT DISCRIMINATOR ===')
print('If G2 is a real channel, it correlates with R5/B5 like G3 does.')
print('If G2 is garbage, its correlation with R5/B5 is ~0 while G3-B5 is high.')
print()

print('--- OLD dumps (the broken ones) ---')
old = {}
for fn in ('raw3_1.bin', 'raw3_2.bin', 'raw_0.bin'):
    old[fn] = stats(load(OUT + '\\' + fn), fn)

print()
print('--- NEW captures ---')
new = {}
for fn in ('cap_conv1.bin', 'cap_conv0.bin'):
    new[fn] = stats(load(OUT + '\\' + fn), fn)

print()
print('=== INTER-FRAME G2 correlation (the strongest garbage test) ===')
print('Garbage re-randomizes every frame => ~0. Real channel => high.')
import subprocess
