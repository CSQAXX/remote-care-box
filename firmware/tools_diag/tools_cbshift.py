import numpy as np

W, H = 320, 240

def load(p):
    d = open(p, "rb").read()
    if len(d) != W * H * 2:
        return None
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)

print("=== 彩条: 每列的主色, 看图案是否在移动 ===")
for tag in ("fin_cb1", "fin_cb2", "fin_cb3"):
    a = load(f"{tag}.bin")
    if a is None: continue
    cols = []
    for x in range(0, W, 20):
        col = a[:, x]
        vals, counts = np.unique(col, return_counts=True)
        cols.append((x, int(vals[np.argmax(counts)])))
    print(f"  {tag}: " + " ".join(f"{x}:{v:04X}" for x, v in cols))

print()
print("=== 彩条帧的行偏移 (图案上下滚动?) ===")
a = load("fin_cb1.bin"); b = load("fin_cb2.bin")
best = None
for dy in range(-8, 9):
    shifted = np.roll(b, dy, axis=0)
    d = np.abs(a - shifted)
    pct = 100 * (d > 4).sum() / (W * H)
    if best is None or pct < best[1]:
        best = (dy, pct)
    if abs(dy) <= 3:
        print(f"  dy={dy:+d}: 差异>4 = {pct:5.2f}%")
print(f"  最佳对齐: dy={best[0]:+d}, 差异>4 = {best[1]:.2f}%")

print()
print("=== 真实场景: 逐 bit 检查 (归一化后) ===")
sc = [load(f"fin_sc{i}.bin") for i in (1, 2, 3)]
for i in range(2):
    a, b = sc[i].astype(np.float64), sc[i+1].astype(np.float64)
    na = a / (a.mean() or 1)
    nb = b / (b.mean() or 1)
    print(f"  sc{i+1} vs sc{i+2}: max|Δ| = {np.abs(na-nb).max():.2f} (归一化单位)")

print()
print("=== 真实场景: 未归一化的逐 bit 翻转 ===")
a, b = sc[0], sc[1]
xor = a ^ b
for bit in range(16):
    n = ((xor >> bit) & 1).sum()
    if n:
        print(f"    bit {bit:2d}: {n}")
