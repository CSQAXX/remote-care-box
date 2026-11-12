#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端音频上行验证
==================

从板子的 /audio/alaw 拉一段真实音频流, 解码成 WAV, 检查:
    1. 码流能拉下来且长度合理
    2. G.711 A-law 解码后是有效 PCM16
    3. 音量统计 —— 静音段 vs 有声段能区分开
    4. 存成 WAV 供人耳试听

用法:
    python tools/audio_e2e_test.py --device 192.168.2.138 --seconds 5
"""
import argparse
import audioop
import json
import os
import struct
import sys
import time
import urllib.request
import wave
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

DEFAULT_DEVICE = "192.168.2.138"
SAMPLE_RATE = 16000


def probe_health(host, timeout=5):
    try:
        with urllib.request.urlopen(f"http://{host}:8080/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def pull_alaw(host, seconds, sample_rate=SAMPLE_RATE):
    """拉取一段 A-law 流。/audio/alaw 是无限流, 必须自己限时截断。"""
    url = f"http://{host}:8080/audio/alaw"
    req = urllib.request.Request(url)
    resp = urllib.request.urlopen(req, timeout=10)
    deadline = time.time() + seconds
    buf = bytearray()
    try:
        while time.time() < deadline:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
    finally:
        resp.close()
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--out", default="audio_e2e.wav")
    args = ap.parse_args()

    print("=" * 60)
    print("音频上行端到端验证")
    print("=" * 60)

    h = probe_health(args.device)
    if not h:
        print("[FAIL] 板子不在线, /health 探不到")
        return 2
    print(f"板子在线  ip={h.get('ip')}  audio_ready={h.get('audio_ready')}  "
          f"rate={h.get('audio_rate')}")
    if not h.get("audio_ready"):
        print("[FAIL] 板子报告 audio_ready=false")
        return 2

    print(f"\n[*] 拉取 {args.seconds}s A-law 流 ...")
    t0 = time.time()
    alaw = pull_alaw(args.device, args.seconds)
    elapsed = time.time() - t0

    if len(alaw) == 0:
        print("[FAIL] 一个字节都没拉到")
        return 2

    expected = int(args.seconds * SAMPLE_RATE)
    print(f"[*] 拉取 {len(alaw)} 字节, 用时 {elapsed:.1f}s")
    print(f"    期望约 {expected} 字节 (16kHz 单声道 A-law)")

    failures = 0

    # 1. 码率
    actual_rate = len(alaw) / max(elapsed, 0.001)
    print(f"    实测码率 {actual_rate:.0f} 字节/s")
    if abs(actual_rate - SAMPLE_RATE) > SAMPLE_RATE * 0.25:
        print("  [FAIL] 码率偏离 16kHz 太多")
        failures += 1
    else:
        print("  [PASS] 码率正常")

    # 2. 解码
    pcm = audioop.alaw2lin(alaw, 2)
    print(f"    解码得到 {len(pcm)} 字节 PCM16 ({len(pcm)//2} 个采样)")

    # 3. 音量分段统计
    frame = SAMPLE_RATE // 10  # 100ms 一段
    rms_list = []
    for i in range(0, len(pcm) - frame, frame):
        chunk = pcm[i:i + frame]
        rms_list.append(audioop.rms(chunk, 2))
    if rms_list:
        rms_sorted = sorted(rms_list)
        q10 = rms_sorted[len(rms_sorted) // 10]
        q50 = rms_sorted[len(rms_sorted) // 2]
        q90 = rms_sorted[len(rms_sorted) * 9 // 10]
        print(f"    音量分布 (100ms 段 RMS): p10={q10} p50={q50} p90={q90}")
        if q50 == 0 and q90 == 0:
            print("  [FAIL] 全程静音 —— 麦克风没工作, 或采集链路断了")
            failures += 1
        else:
            print("  [PASS] 有有效音频信号")
        if q90 > 400:
            print(f"  [PASS] 最大段 RMS={q90} (有明显的声/响交替)")
        else:
            print(f"  [WARN] 最大段 RMS={q90} 偏低, 可能是环境安静或增益不足")
    else:
        print("  [FAIL] 数据量不足以做分段统计")
        failures += 1

    # 4. 存 WAV
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    size = os.path.getsize(args.out)
    print(f"\n[*] 已保存 {args.out} ({size} 字节), {len(pcm)//2/SAMPLE_RATE:.1f}s "
          f"@ {SAMPLE_RATE}Hz 单声道")
    print("    用 Windows 媒体播放器或 Audacity 打开即可试听")

    print("\n" + "=" * 60)
    if failures:
        print(f"结论: [FAIL] {failures} 项不通过")
        return 1
    print("结论: [PASS] 音频上行链路正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
