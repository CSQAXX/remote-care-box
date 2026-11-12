"""
AI 网关三段实测 (对真实运行的网关发请求)。

覆盖:
  1. ASR 段: 上传一段 A-law 语音 -> 拿到识别文本
  2. LLM 段: /v1/chat 纯文本 -> 拿到回复文本
  3. TTS 段: 文本 -> PCM16 16k 单声道
  4. 整轮: /v1/turn 一把梭 (音频进 -> 语音出)

没有云 API key 时自动落到 mock, 所以【链路本身】始终能验证通。

用法:
    python live_test.py [网关地址]
    # 默认 http://127.0.0.1:18000
"""
import hashlib
import hmac
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18000").rstrip("/")
DEVICE_ID = os.environ.get("KANHU_TEST_DEVICE", "kanhu-dev-001")
DEVICE_SECRET = os.environ.get("KANHU_TEST_SECRET", "kanhu-dev-secret-001")

RESULTS = []


def check(name, fn):
    try:
        msg = fn()
        RESULTS.append((True, name, msg))
        print("  [OK] %-28s %s" % (name, msg))
    except Exception as e:
        RESULTS.append((False, name, "%s: %s" % (type(e).__name__, e)))
        print("  [FAIL] %-28s %s: %s" % (name, type(e).__name__, e))


# --- A-law 编码 (和固件 g711.c 一致) --------------------------------------
SEG_AEND = [0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF]


def alaw_encode(sample):
    pcm = sample >> 3
    mask = 0xD5 if pcm >= 0 else 0x55
    if pcm < 0:
        pcm = -pcm
    seg = 8
    for i, e in enumerate(SEG_AEND):
        if pcm <= e:
            seg = i
            break
    aval = seg << 4
    if seg < 2:
        aval |= (pcm >> 1) & 0x0F
    else:
        aval |= (pcm >> seg) & 0x0F
    return (aval ^ mask) & 0xFF


def make_alaw(secs=1.0, freq=220.0, rate=16000):
    """生成一段带音调的单声道 A-law 字节流 (当测试语音)。"""
    n = int(secs * rate)
    out = bytearray()
    for i in range(n):
        v = int(8000 * (i * freq / rate % 1.0 - 0.5) * 2)
        out.append(alaw_encode(v))
    return bytes(out)


def sign(device_id, secret, ts_ms, body):
    digest = hashlib.sha256(body).hexdigest()
    canonical = "%s|%d|%s" % (device_id, ts_ms, digest)
    mac = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).digest()
    return mac[:16].hex()


def post_turn(body, timeout=60):
    ts = int(time.time() * 1000)
    req = urllib.request.Request(
        BASE + "/v1/turn", data=body, method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "X-Device-Id": DEVICE_ID,
            "X-Timestamp": str(ts),
            "X-Signature": sign(DEVICE_ID, DEVICE_SECRET, ts, body),
        })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def post_json(path, payload, timeout=60):
    data = json_bytes(payload)
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def json_bytes(obj):
    import json
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def get_json(path, timeout=20):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        import json
        return json.loads(r.read().decode("utf-8"))


# --- 三段实测 ---------------------------------------------------------------

def t_health():
    h = get_json("/v1/health")
    assert h.get("ok") is True, h
    return ("ASR=%s LLM=%s TTS=%s mongo=%s" % (
        "配置" if h["asr"]["configured"] else "mock",
        "配置" if h["llm"]["configured"] else "mock",
        h["tts"]["provider"], h.get("mongo")))


def t_asr_stage():
    """ASR 段: 直接走 /v1/turn, 从响应头里取识别文本。"""
    body = make_alaw(1.0)
    pcm, hdr = post_turn(body)
    text = urllib.parse.unquote(hdr.get("X-User-Text", ""))
    assert text, "响应头没有 X-User-Text"
    return "识别: %s (%d 字节 PCM 回包)" % (text, len(pcm))


def t_llm_stage():
    """LLM 段: /v1/chat 纯文本。"""
    import json
    out = json.loads(post_json("/v1/chat", {"text": "你好, 现在几点了?",
                                            "device_id": "live-test"}).decode())
    reply = out.get("reply_text") or ""
    assert reply, out
    return "回复: %s" % reply[:60]


def t_tts_stage():
    """TTS 段: 响应体就是 PCM16 16k 单声道。"""
    body = make_alaw(0.5)
    pcm, hdr = post_turn(body)
    assert len(pcm) % 2 == 0, "PCM 长度不是偶数 (不是 16bit 对齐)"
    secs = len(pcm) / 2 / 16000
    assert secs > 0.2, "TTS 输出太短: %.2f 秒" % secs
    return "%.2f 秒 PCM16 16k 单声道 (%d 字节)" % (secs, len(pcm))


def t_full_turn():
    """整轮: 音频进 -> 语音出, 一次请求走完全链路。"""
    body = make_alaw(1.5)
    pcm, hdr = post_turn(body)
    u = urllib.parse.unquote(hdr.get("X-User-Text", ""))
    r = urllib.parse.unquote(hdr.get("X-Reply-Text", ""))
    assert u and r, (u, r)
    assert hdr.get("X-Rate") == "16000", hdr
    assert hdr.get("X-Channels") == "1", hdr
    return "识别「%s」-> 回复「%s」-> %.2f 秒语音" % (u[:20], r[:30], len(pcm) / 2 / 16000)


def t_auth_reject():
    """鉴权必须挡住错误签名。"""
    ts = int(time.time() * 1000)
    body = make_alaw(0.2)
    req = urllib.request.Request(
        BASE + "/v1/turn", data=body, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-Device-Id": DEVICE_ID, "X-Timestamp": str(ts),
                 "X-Signature": "00" * 16})
    try:
        urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as e:
        assert e.code in (401, 403), "错误签名居然通过了: HTTP %d" % e.code
        return "错误签名被拒 (HTTP %d)" % e.code
    raise AssertionError("错误签名居然通过了")


if __name__ == "__main__":
    print("=" * 60)
    print("  AI 网关三段实测 -> %s" % BASE)
    print("=" * 60)
    check("健康检查", t_health)
    check("ASR 段", t_asr_stage)
    check("LLM 段", t_llm_stage)
    check("TTS 段", t_tts_stage)
    check("整轮对话", t_full_turn)
    check("鉴权拦截", t_auth_reject)
    ok = sum(1 for r in RESULTS if r[0])
    print("=" * 60)
    print("通过 %d / %d" % (ok, len(RESULTS)))
    sys.exit(0 if ok == len(RESULTS) else 1)
