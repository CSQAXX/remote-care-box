import numpy as np
from PIL import Image

W, H = 320, 240

def load(p):
    return np.frombuffer(open(p, "rb").read(), dtype=">u2").reshape(H, W).astype(np.int32)

def to_img(arr):
    r = ((arr >> 11) & 0x1F) * 255 // 31
    g = ((arr >> 5) & 0x3F) * 255 // 63
    bl = (arr & 0x1F) * 255 // 31
    return np.dstack([r, g, bl]).astype(np.uint8)

def analyze(p, label):
    arr = load(p)
    print(f"=== {label} ({p}) ===")
    # per-COLUMN: colorbar 是竖条, 每一列内部应该完全一致
    tot_bad = 0
    tot_px = 0
    worst = []
    for x in range(W):
        col = arr[:, x]
        vals, counts = np.unique(col, return_counts=True)
        dom = vals[np.argmax(counts)]
        bad = (col != dom).sum()
        tot_bad += bad
        tot_px += H
        worst.append((bad, x, int(dom)))
    print(f"  列内不一致像素总数: {tot_bad}/{tot_px} = {100*tot_bad/tot_px:.2f}%")
    worst.sort(reverse=True)
    print(f"  最差 5 列: {[(f'x{x}', b, f'0x{d:04X}') for b, x, d in worst[:5]]}")
    print(f"  最好 5 列: {[(f'x{x}', b, f'0x{d:04X}') for b, x, d in worst[-5:]]}")

    # 每 40 列一组, 看彩条边界
    print("  每 40 列的主色:")
    for x0 in range(0, W, 40):
        block = arr[:, x0:x0+40]
        vals, counts = np.unique(block, return_counts=True)
        dom = vals[np.argmax(counts)]
        print(f"    x{x0:3d}-{x0+39:3d}: dominant=0x{dom:04X} ({100*counts.max()/block.size:.1f}%)")

    Image.fromarray(to_img(arr)).resize((W*2, H*2), Image.NEAREST).save(label + ".png")
    return arr

if __name__ == "__main__":
    analyze("cb_b.bin", "cbB")
