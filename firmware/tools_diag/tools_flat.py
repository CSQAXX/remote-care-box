import numpy as np

W, H = 320, 240
data = open("raw_frame.bin", "rb").read()
arr = np.frombuffer(data, dtype=">u2").reshape(H, W).astype(np.int32)

r5 = (arr >> 11) & 0x1F
g6 = (arr >> 5) & 0x3F
b5 = arr & 0x1F

def grad(a):
    return np.abs(np.diff(a, axis=1)).sum() + np.abs(np.diff(a, axis=0)).sum()

best = None
for y in range(0, H - 16, 4):
    for x in range(0, W - 16, 4):
        g = grad(arr[y:y+16, x:x+16])
        if best is None or g < best[0]:
            best = (g, y, x)
g, y0, x0 = best
print(f"flattest 16x16 block at ({x0},{y0}) grad={g}")
for name, ch in [("R5", r5), ("G6", g6), ("B5", b5)]:
    v = ch[y0:y0+16, x0:x0+16]
    print(f"\n{name} flat block: min={v.min()} max={v.max()} std={v.std():.3f}")
    print(v)

print("\n\n=== isolated single-pixel spike detection (full frame) ===")
def spikes(ch, thr):
    p = np.pad(ch.astype(np.int32), 1, mode='edge')
    nb = np.zeros_like(ch, dtype=np.int32)
    for dy in (0,1,2):
        for dx in (0,1,2):
            if dy==1 and dx==1: continue
            nb += p[dy:dy+ch.shape[0], dx:dx+ch.shape[1]]
    nb /= 8.0
    d = np.abs(ch.astype(np.int32) - nb)
    return (d > thr).sum(), d

for name, ch, thr in [("R5", r5, 6), ("G6", g6, 6), ("B5", b5, 6)]:
    cnt, d = spikes(ch, thr)
    print(f"  {name}: isolated spikes(>{thr}) = {cnt}  ({100*cnt/ch.size:.2f}%)  max_dev={d.max():.0f}")

print("\n=== where are the R5/B5 spikes? (locations of top 20 dev) ===")
_, d = spikes(r5, 6)
ys, xs = np.where(d > 6)
print("  R5 spike count:", len(ys))
for i in range(min(20, len(ys))):
    y, x = ys[i], xs[i]
    print(f"    ({x:3d},{y:3d}) R5={r5[y,x]:2d} nb={r5[max(0,y-1):y+2,max(0,x-1):x+2].mean():.1f} G6={g6[y,x]:2d} B5={b5[y,x]:2d}")
