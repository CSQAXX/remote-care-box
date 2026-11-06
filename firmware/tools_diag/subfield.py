import numpy as np, glob

fs = sorted(glob.glob(r'D:\Software\kanhu\remote-care-box\raw2_*.bin'))
d = open(fs[0], 'rb').read()
i = d.find(b'\r\n\r\n')
b = d[i + 4:i + 4 + 153600]
a = np.frombuffer(b, dtype=np.uint8).reshape(240, 320, 2)
H = a[:, :, 0].astype(np.int64)
L = a[:, :, 1].astype(np.int64)

print("=== What does the 'hi' plane look like alone? (as a grayscale image) ===")
def lapstd(g):
    g = np.asarray(g, dtype=np.float64)
    return float((g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1]
                  - g[1:-1, :-2] - g[1:-1, 2:]).std())

print("hi plane laplacian      : %.2f" % lapstd(H))
print("lo plane laplacian      : %.2f" % lapstd(L))
print("hi >> 3 (R5) laplacian  : %.2f" % lapstd(H >> 3))
print("hi & 7 (G3) laplacian   : %.2f" % lapstd(H & 7))
print("lo >> 5 (G2) laplacian  : %.2f" % lapstd(L >> 5))
print("lo & 0x1F (B5) laplacian: %.2f" % lapstd(L & 0x1F))

print("\n=== Key: which sub-field is noisy? ===")
print("R5 (hi>>3)  adj-diff: %.2f" % np.abs((H >> 3)[:, 1:] - (H >> 3)[:, :-1]).mean())
print("G3 (hi&7)   adj-diff: %.2f" % np.abs((H & 7)[:, 1:] - (H & 7)[:, :-1]).mean())
print("G2 (lo>>5)  adj-diff: %.2f" % np.abs((L >> 5)[:, 1:] - (L >> 5)[:, :-1]).mean())
print("B5 (lo&0x1F) adj-diff: %.2f" % np.abs((L & 0x1F)[:, 1:] - (L & 0x1F)[:, :-1]).mean())

print("\n=== If the stream were YUYV, then in EVERY 4-byte group ===")
print("   bytes: Y0 U Y1 V  -> U at idx%4==1, V at idx%4==3")
flat = a.reshape(-1, 2)
b01 = flat[:, 0].reshape(-1)   # even byte of each pixel
b12 = flat[:, 1].reshape(-1)   # odd byte
# pixel n's bytes: (2n, 2n+1). YUYV groups of 4 bytes = 2 pixels.
print("   if YUYV: bytes 0,2,4.. = Y ; bytes 1,3,5.. = U,V,U,V")
Y_cand = b01[0::2]   # pixels 0,2,4 -> bytes 0,4,8
print("   Y-candidate adj-diff : %.2f" % np.abs(Y_cand[1:] - Y_cand[:-1]).mean())
U_cand = b12[0::2]   # bytes 1,5,9
V_cand = b12[1::2]   # bytes 3,7,11
print("   U-candidate adj-diff : %.2f" % np.abs(U_cand[1:] - U_cand[:-1]).mean())
print("   V-candidate adj-diff : %.2f" % np.abs(V_cand[1:] - V_cand[:-1]).mean())

print("\n=== if YUYV, chroma is SHARED between pixel pairs ===")
print("   -> U must be constant over the pair, i.e. b12[0::2] and b12[2::2] differ (different pairs)")
print("   U[pair k] vs U[pair k+1] adj-diff: %.2f" % np.abs(U_cand[1:] - U_cand[:-1]).mean())
print("   -> and U within a pair doesn't exist (one U per 2 pixels)")

print("\n=== TEST: does the data look like YUYV with BYTE SWAP (U/V swapped)? ===")
print("   swap U and V -> just changes hue, won't fix noise")
print("   swap HI/LO  -> then 'lo' becomes luma (should be smooth!)")
H2 = b12.copy()   # even idx becomes old odd
L2 = b01.copy()
print("   after swap: new-hi adj-diff %.2f, new-lo adj-diff %.2f" % (
    np.abs(H2[:, 1:] - H2[:, :-1]).mean(), np.abs(L2[:, 1:] - L2[:, :-1]).mean()))
