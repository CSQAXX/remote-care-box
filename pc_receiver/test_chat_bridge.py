#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
对话桥离线验收：不接板子、不联网、不需要任何 API key。

    python test_chat_bridge.py

覆盖：A-law 解码 / VAD 断句 / 重采样 / Mock 全链路 / 控制台页面与四个接口。
连板子的端到端验收请用 test_console.py + 实机「④ AI 对话」按钮。
"""
import io
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chat_bridge as cb          # noqa: E402
import pc_console as pc           # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append((name, bool(ok), extra))
    print("%s %-34s %s" % ("[PASS]" if ok else "[FAIL]", name, extra))


# ---- 1. A-law 解码：内置表与固件 g711.c 同源，256 个码字全解得出 ----
raw = bytes(range(256))
pcm = cb.alaw_to_pcm(raw)
check("A-law 解码 256 码字", len(pcm) == 512 and any(pcm),
      "%d 字节" % len(pcm))
# 与 audioop（若存在）对照，防止表和 ITU 参考实现有偏差
try:
    import audioop
    ref = audioop.alaw2lin(raw, 2)
    diff = sum(1 for i in range(256)
               if struct.unpack_from("<h", ref, i * 2)[0] !=
               struct.unpack_from("<h", pcm, i * 2)[0])
    check("A-law 对照 audioop", diff == 0, "%d/256 不一致" % diff)
except ImportError:
    print("[SKIP] 本机无 audioop（Python 3.13+），内置解码表已独立生效")

# ---- 2. VAD：1s 人声 + 2s 静音，应切出一句且长度合理 ----
vad = cb.VAD()
speech = cb.tone_pcm(1.0, 440, 12000)
silence = bytes(cb.SAMPLE_RATE * 2)
stream = speech + silence
utterance = None
for off in range(0, len(stream), 640):
    utterance = vad.feed(stream[off:off + 640])
    if utterance:
        break
secs = len(utterance) / (cb.SAMPLE_RATE * 2) if utterance else 0
check("VAD 断句", utterance is not None and 0.4 <= secs <= 1.5,
      "%.2fs / %d 字节" % (secs, len(utterance or b"")))

# ---- 3. 静音不应触发对话（防误唤醒） ----
vad2 = cb.VAD()
noise_only = None
for off in range(0, len(silence), 640):
    noise_only = vad2.feed(silence[off:off + 640])
    if noise_only:
        break
check("静音不误触发", noise_only is None)

# ---- 4. Mock 大脑全链路 ----
info = cb.MockBrain().reply(utterance or speech)
check("Mock 对话全链路",
      bool(info["user_text"]) and bool(info["reply_text"]) and
      len(info["pcm"]) > 0 and not info["skip"],
      "下行 %d 字节" % len(info["pcm"]))

# ---- 5. 24k -> 16k 重采样（小智服务端下行常见 24k） ----
p24 = cb.tone_pcm(1.0, 440, 8000)
p16 = cb.resample_pcm(p24, 24000, 16000)
check("24k -> 16k 重采样", 0 < len(p16) < len(p24) and len(p16) % 2 == 0,
      "%d -> %d 字节" % (len(p24), len(p16)))

# ---- 6. 控制台页面挂上了第四张卡片 ----
class FakeBoard:
    device = "127.0.0.1"


pc.BOARD = FakeBoard()


def call(path, method="GET", body=b""):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    env = {"PATH_INFO": path, "REQUEST_METHOD": method,
           "CONTENT_LENGTH": str(len(body)), "QUERY_STRING": "",
           "wsgi.input": io.BytesIO(body)}
    out = b"".join(pc.app(env, start_response))
    return captured, out


_, html = call("/")
check("页面含 AI 对话卡片",
      "④ AI 对话".encode("utf-8") in html and b"btnChat" in html)

st, body = call("/api/chat/status")
d = json.loads(body.decode("utf-8"))
check("接口 /api/chat/status",
      st["status"].startswith("200") and "backend" in d and
      "config_problems" in d and "rounds" in d,
      "后端=%s 状态=%s" % (d.get("backend"), d.get("state")))

_, body = call("/api/chat/start", "POST")
d = json.loads(body.decode("utf-8"))
# 没填 key 时必须明确拒绝并给出待补项，而不是悄悄起一个跑不通的线程
check("无 key 时拒绝启动", d.get("ok") is False and bool(d.get("problems")),
      "; ".join(d.get("problems", []))[:80])

_, body = call("/api/chat/say", "POST", "你好".encode("utf-8"))
d = json.loads(body.decode("utf-8"))
check("接口 /api/chat/say", d.get("ok") is False and bool(d.get("problems")))

_, body = call("/api/chat/stop", "POST")
d = json.loads(body.decode("utf-8"))
check("接口 /api/chat/stop", d.get("ok") is True)

# ---- 7. 自检模式（CLI 自带） ----
check("CLI selftest 可调用", callable(cb.selftest))

failed = [n for n, ok, _ in results if not ok]
print("\n%d/%d 通过" % (len(results) - len(failed), len(results)))
if failed:
    print("失败项: " + ", ".join(failed))
sys.exit(1 if failed else 0)
