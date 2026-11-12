#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三段链路实测: ASR / LLM / TTS + 完整 /v1/turn。

生成一段 G.711 A-law 音频 (1kHz 正弦, 模拟"人声"能量),
带上 HMAC-SHA256 签名打 /v1/turn, 校验返回的 PCM16。

用法: python3 diag_turn.py <base_url> <device_id> <device_secret>
"""
import hashlib
import hmac
import json
import math
import struct
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18000"
DEV = sys.argv[2] if len(sys.argv) > 2 else "kanhu-dev-001"
SECRET = sys.argv[3] if len(sys.argv) > 3 else "kanhu-dev-secret-001"


def build_alaw_table():
    """与固件 firmware/main/g711.c 的 A-law 编码完全一致。"""
    table = []
    for i in range(256):
        a_val = i ^ 0x55
        t = (a_val & 0x0F) << 4
        seg = (a_val & 0x70) >> 4
        if seg == 0:
            t += 8
        elif seg == 1:
            t += 0x108
        else:
            t = (t + 0x108) << (seg - 1)
        table.append(t if (a_val & 0x80) else -t)
    return table


ALAW = build_alaw_table()

# A-law 13bit 线性值各段上界 (与 g711.c 的 seg_aend 一致)
SEG_AEND = [0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF]


def pcm_to_alaw(pcm):
    """逐位复刻 g711_alaw_encode()。

    关键: G.711 用 13bit 线性值 (sample >> 3), 正负用不同的掩码
    (0xD5 = 符号位 1 / 0x55 = 符号位 0), 最后 xor 掩码。
    """
    out = bytearray()
    for s in pcm:
        pcmv = s >> 3
        if pcmv >= 0:
            mask = 0xD5
        else:
            mask = 0x55
            pcmv = -pcmv - 1

        seg = 8
        for i in range(8):
            if pcmv <= SEG_AEND[i]:
                seg = i
                break
        if seg >= 8:
            out.append(0x7F ^ mask)
            continue

        aval = seg << 4
        if seg < 2:
            aval |= (pcmv >> 1) & 0x0F
        else:
            aval |= (pcmv >> seg) & 0x0F
        out.append((aval ^ mask) & 0xFF)
    return bytes(out)


def make_tone(seconds=2.0, rate=16000, freq=220.0):
    n = int(rate * seconds)
    pcm = []
    for i in range(n):
        v = int(6000 * math.sin(2 * math.pi * freq * i / rate))
        pcm.append(v)
    return pcm


def signed_headers(body):
    ts = int(time.time() * 1000)
    digest = hashlib.sha256(body).hexdigest()
    src = "%s|%d|%s" % (DEV, ts, digest)
    sig = hmac.new(SECRET.encode(), src.encode(), hashlib.sha256).hexdigest()[:32]
    return {
        "X-Device-Id": DEV,
        "X-Timestamp": str(ts),
        "X-Signature": sig,
        "Content-Type": "application/octet-stream",
    }


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def main():
    out = {}

    # --- 1. health ---
    st, _, body = http(BASE + "/v1/health")
    out["health"] = {"status": st, "json": json.loads(body.decode())}

    # --- 2. chat (LLM) ---
    payload = json.dumps({"text": "你好，请用一句话介绍你自己", "device_id": DEV}).encode()
    st, _, body = http(BASE + "/v1/chat", payload, {"Content-Type": "application/json"})
    out["chat"] = {"status": st, "json": json.loads(body.decode())}

    # --- 3. turn (ASR -> LLM -> TTS) ---
    pcm = make_tone(2.0)
    alaw = pcm_to_alaw(pcm)
    st, hdrs, body = http(BASE + "/v1/turn", alaw, signed_headers(alaw))
    rec = {"status": st, "alaw_bytes": len(alaw), "pcm_bytes": len(body)}
    for h in ("X-User-Text", "X-Reply-Text"):
        if h in hdrs:
            rec[h] = hdrs[h]
    if st == 200:
        samples = len(body) // 2
        rec["samples"] = samples
        rec["seconds"] = round(samples / 16000.0, 2)
        if samples > 0:
            vals = struct.unpack("<%dh" % samples, body[:samples * 2])
            peak = max(abs(v) for v in vals)
            rec["peak"] = peak
            rec["nonzero"] = sum(1 for v in vals if v != 0)
    else:
        rec["body"] = body.decode(errors="replace")[:300]
    out["turn"] = rec

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
