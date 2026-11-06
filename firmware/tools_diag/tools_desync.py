"""
行同步 (line desync) 检测 + 配置扫描。

传感器彩条是【水平色带】: 每一整行的颜色都一样 (除噪声)。
所以每一行的 "中位色" 就是一个稳定的签名。
如果 DVP 采集丢行/多行, 签名会整体上下平移 —— 这正是花屏的机制。

指标:
  1) 行签名错位 (row_shift): 两帧签名互相关, 最佳整数行偏移
  2) 行签名一致率 (row_match): 平移对齐后签名相同的行比例
  3) 位翻转率 (bitflip): 对齐后逐像素 bit 差异
"""
import numpy as np
import urllib.request, time, serial, os

IP = "192.168.2.138"
W, H = 320, 240
os.chdir(r"D:\Software\kanhu\remote-care-box\firmware")


def open_clean(drain_s=2.0):
    s = serial.Serial("COM3", 115200, timeout=0.2)
    try:
        s.dtr = False; s.rts = False
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < drain_s:
        try: s.readline()
        except Exception: pass
    return s


def cmd(s, c, wait=2.0):
    s.write((c + "\n").encode()); s.flush()
    t0 = time.time()
    while time.time() - t0 < wait:
        try: s.readline()
        except Exception: pass


def http_raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int32)


def row_sig(a):
    return np.median(a, axis=1).astype(np.int64)


def count_bands(sig):
    return int((np.diff(sig) != 0).sum()) + 1


def best_shift(s1, s2, maxs=48):
    n = len(s1)
    best = (0, 1e18)
    for sh in range(-maxs, maxs + 1):
        y1a = maxs
        if y1a + (n - 2 * maxs) > n:
            continue
        seg = slice(y1a, y1a + (n - 2 * maxs))
        e = np.abs(s1[seg] - s2[seg]).mean()
        if e < best[1]:
            best = (sh, e)
    return best


def analyze_pair(a, b):
    s1, s2 = row_sig(a), row_sig(b)
    sh, _ = best_shift(s1, s2)
    if sh > 0:
        aa, bb = a[sh:], b[:H - sh]
    elif sh < 0:
        aa, bb = a[:H + sh], b[-sh:]
    else:
        aa, bb = a, b
    sig_a, sig_b = row_sig(aa), row_sig(bb)
    row_match = float((sig_a == sig_b).mean())
    xor = aa ^ bb
    bitflip = float(sum(((xor >> k) & 1).sum() for k in range(16))) / 16 / (W * H)
    return sh, row_match, bitflip, count_bands(s1), count_bands(s2)


def run_combo(s, label, pda=None, pclk=None, mode=None, spiclk=None, n=5):
    if mode is not None:
        cmd(s, f"mode {mode}", 6.0)
    if pda is not None:
        cmd(s, f"pda {pda}", 8.0)
    if pclk is not None:
        cmd(s, f"pclk {pclk}", 2.0)
    if spiclk is not None:
        cmd(s, f"spiclk {spiclk}", 5.0)
    cmd(s, "cbar 1", 2.0)
    time.sleep(4.0)
    frames = []
    for i in range(n):
        try:
            frames.append(load(http_raw()))
        except Exception as e:
            print(f"    抓帧失败: {e}")
        time.sleep(0.35)
    cmd(s, "cbar 0", 1.0)
    if len(frames) < 2:
        print(f"  {label:22s} 抓帧不足")
        return None
    res = [analyze_pair(frames[i], frames[i + 1]) for i in range(len(frames) - 1)]
    shifts = [r[0] for r in res]
    matches = [r[1] for r in res]
    flips = [r[2] for r in res]
    bands = [(r[3], r[4]) for r in res]
    print(f"  {label:22s} 错位={shifts}  行一致={np.mean(matches)*100:5.1f}%  "
          f"bit翻转={np.mean(flips)*100:5.2f}%  带数={bands}", flush=True)
    return dict(label=label, shift=shifts, match=float(np.mean(matches)),
                flip=float(np.mean(flips)), bands=bands)


def main():
    s = open_clean(2.5)
    out = []
    print("=== 基线 mode0 pda1 pclk1 ===", flush=True)
    out.append(run_combo(s, "mode0 pda1 pclk1", mode=0, pda=1, pclk=1))
    print("=== PCLK 扫描 (mode0 pda1) ===", flush=True)
    for p in (0, 2, 3):
        out.append(run_combo(s, f"mode0 pda1 pclk{p}", mode=0, pda=1, pclk=p))
    print("=== PSRAM DMA 开关 ===", flush=True)
    out.append(run_combo(s, "mode0 pda0 pclk1", mode=0, pda=0, pclk=1))
    print("=== 组合 ===", flush=True)
    for pda in (0, 1):
        for p in (2, 3):
            out.append(run_combo(s, f"mode0 pda{pda} pclk{p}", mode=0, pda=pda, pclk=p))
    print("=== 无转换器 mode1 ===", flush=True)
    out.append(run_combo(s, "mode1 pda0 pclk2", mode=1, pda=0, pclk=2))
    out.append(run_combo(s, "mode1 pda1 pclk2", mode=1, pda=1, pclk=2))
    s.close()
    print()
    print("=== 汇总 (按行签名一致率排序) ===")
    for o in sorted([x for x in out if x], key=lambda x: -x["match"]):
        print(f"  {o['label']:22s} 行一致 {o['match']*100:5.1f}%  bit翻转 {o['flip']*100:5.2f}%  错位 {o['shift']}")
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
