import numpy as np

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

for f in ("cbhttp0.bin", "cbhttp1.bin", "cbhttp2.bin", "cbhttp3.bin"):
    a = load(f)
    print(f"=== {f} ===")
    # 每行: 该行有多少像素与【该列在整个下半区(128-239)的干净值】不同
    clean_ref = a[128:240, :]          # 用干净区做参考
    colmed = np.median(clean_ref, axis=0)  # 每列的干净值
    colmed = colmed.astype(np.int32)
    bad_per_row = []
    for y in range(H):
        bad = (a[y] != colmed).sum()
        bad_per_row.append(bad)
    bp = np.array(bad_per_row)
    print("  每行坏点数 (前 140 行):")
    for y in range(0, 140, 4):
        print(f"    行{y:3d}: {bp[y]:3d}/{W}")
    print(f"  坏点数 > 10 的行: {np.where(bp > 10)[0].tolist()}")
    print(f"  坏点数 <= 10 的行: {np.where(bp <= 10)[0].tolist()}")
    print()
