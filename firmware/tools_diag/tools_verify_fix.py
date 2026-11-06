"""
【修复验证】AGC 关掉之后, 花屏和闪烁是否真的消失了。

判据 (对应用户看到的主观现象):
  1) hf_p90 恒定 < 256        -> 没有满屏雪花 (花屏消失)
  2) frame_luma 恒定          -> 亮度不跳 (闪屏消失)
  3) 帧间差异接近 0           -> 画面稳定
  4) A/B 对照: agc 1 应该能复现花屏, agc 0 应该干净
"""
import numpy as np
import urllib.request, time, json, serial, os

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


def cmd(s, c, wait=3.0):
    s.write((c + "\n").encode()); s.flush()
    t0 = time.time()
    while time.time() - t0 < wait:
        try: s.readline()
        except Exception: pass


def raw():
    return urllib.request.urlopen(f"http://{IP}:8080/raw?capture=1", timeout=30).read()


def health():
    return json.loads(urllib.request.urlopen(f"http://{IP}:8080/health", timeout=10).read().decode())


def load(d):
    return np.frombuffer(d, dtype=">u2").reshape(H, W).astype(np.int64)


def hf_p90(a):
    dh = np.abs(np.diff(a.astype(np.float64), axis=1))
    return float(np.percentile(dh, 90))


def run(s, label, n=14):
    frames = []
    print(f"  --- {label} ---")
    for i in range(n):
        d = raw()
        a = load(d)
        h = health()
        frames.append(a)
        print(f"    帧{i:2d} 亮度={a.mean():8.0f}  本地hf_p90={hf_p90(a):7.1f}  "
              f"固件hf_p90={h.get('hf_p90')}  固件luma={h.get('frame_luma')}  "
              f"花屏计数={h.get('snow_frames')}/{h.get('snow_total')}")
        time.sleep(0.25)

    lums = [f.mean() for f in frames]
    hfs = [hf_p90(f) for f in frames]
    bad = sum(1 for x in hfs if x > 256)
    lum_spread = max(lums) - min(lums)

    # 帧间差异 (只和前一帧比)
    diffs = []
    for i in range(len(frames) - 1):
        x = frames[i] ^ frames[i + 1]
        d = sum(((x >> k) & 1).sum() for k in range(16)) / 16 / (W * H)
        diffs.append(d * 100)

    print(f"  => 花屏帧 {bad}/{len(frames)}  亮度波动 {lum_spread:.0f}  "
          f"帧间bit翻转 平均 {np.mean(diffs):.2f}% 最大 {max(diffs):.2f}%")
    return bad, lum_spread, np.mean(diffs)


def main():
    s = open_clean(2.5)
    time.sleep(3.0)

    print("=== A) 修复后 (默认: 手动曝光) ===", flush=True)
    a = run(s, "agc 0 (手动, 默认)")

    print()
    print("=== B) 对照: 切回自动 (agc 1) —— 应该复现花屏 ===", flush=True)
    cmd(s, "agc 1", 3.0)
    time.sleep(4.0)
    b = run(s, "agc 1 (自动)")

    print()
    print("=== C) 再切回手动 (agc 0) —— 应该恢复干净 ===", flush=True)
    cmd(s, "agc 0", 3.0)
    time.sleep(4.0)
    c = run(s, "agc 0 (手动, 恢复)")

    s.close()
    print()
    print("==================== 结论 ====================")
    print(f"  手动曝光: 花屏 {a[0]}/{14} 帧, 亮度波动 {a[1]:.0f}, 帧间翻转 {a[2]:.2f}%")
    print(f"  自动曝光: 花屏 {b[0]}/{14} 帧, 亮度波动 {b[1]:.0f}, 帧间翻转 {b[2]:.2f}%")
    print(f"  恢复手动: 花屏 {c[0]}/{14} 帧, 亮度波动 {c[1]:.0f}, 帧间翻转 {c[2]:.2f}%")
    ok = (a[0] == 0 and a[1] < 2000) and (b[0] > 3)
    print(f"  修复有效: {'是' if ok else '否'}")
    print("ALL DONE")


if __name__ == "__main__":
    main()
