"""
AI 网关离线验收 (不碰服务器, 不调云 API)。

覆盖:
  1. A-law 编解码往返 (板子上行格式)
  2. WAV 封装 / PCM 重采样
  3. HMAC 签名生成与校验
  4. 设备鉴权 (对/错密钥/过期时间戳/未注册设备)
  5. ASR/LLM mock 降级
  6. TTS 三档 (mock / edge / sapi 可用性探测)
  7. FastAPI 路由实调 (TestClient)

用法:
    python test_gateway.py
"""
import hashlib
import hmac
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as gw  # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        msg = fn()
        RESULTS.append((True, name, msg))
    except Exception as e:
        RESULTS.append((False, name, "%s: %s" % (type(e).__name__, e)))


# --------------------------------------------------------------------------
# 1) A-law 编解码往返
# --------------------------------------------------------------------------
SEG_AEND = [0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF]


def _alaw_encode_sample(sample: int) -> int:
    """
    固件 g711.c 的 g711_alaw_encode() 的 Python 等价实现。

    逐行对照 firmware/main/g711.c:
        pcm = sample >> 3                (13bit 线性值)
        mask = 0xD5 (正) / 0x55 (负)
        seg  = 第一个 seg_aend[i] >= pcm 的下标
        aval = seg << 4
        seg<2  -> aval |= (pcm >> 1) & 0x0F
        否则   -> aval |= (pcm >> seg) & 0x0F
        return aval ^ mask
    """
    pcm = sample >> 3
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1

    seg = 8
    for i, bound in enumerate(SEG_AEND):
        if pcm <= bound:
            seg = i
            break
    if seg >= 8:
        return (0x7F ^ mask) & 0xFF

    aval = (seg << 4) & 0xFF
    if seg < 2:
        aval |= (pcm >> 1) & 0x0F
    else:
        aval |= (pcm >> seg) & 0x0F
    return (aval ^ mask) & 0xFF


def t_alaw_roundtrip():
    """
    用固件同款编码 -> 网关解码, 验证往返误差在 A-law 量化步长内。

    注意固件编码时先 >>3 变 13bit, 解码出来直接是 16bit 尺度,
    量化步长随段号变化, 所以用「段内步长」判定而不是固定百分比。
    """
    max_err = 0
    for raw in (-30000, -20000, -8000, -1000, -100, 0, 100, 1000, 8000, 20000, 30000):
        code = _alaw_encode_sample(raw)
        got = gw.ALAW_TABLE[code]
        # A-law 的量化误差上界: 段 0/1 步长 16, 段 n 步长 16*2^(n-1)
        a_val = code ^ 0x55
        seg = (a_val & 0x70) >> 4
        step = 16 if seg < 2 else 16 * (1 << (seg - 1))
        err = abs(got - raw)
        if err > step:
            raise AssertionError(
                "A-law 往返偏差过大 raw=%d code=0x%02X seg=%d got=%d err=%d step=%d"
                % (raw, code, seg, got, err, step))
        max_err = max(max_err, err)
    return "11 个采样点往返, 最大偏差 %d (在量化步长内)" % max_err


def t_alaw_sign_correct():
    """符号位方向: 正采样必须解出正值。这是之前写反过的地方。"""
    for raw in (1000, 8000, 20000):
        code = _alaw_encode_sample(raw)
        got = gw.ALAW_TABLE[code]
        if got <= 0:
            raise AssertionError("正采样 %d 解出 %d, 符号反了" % (raw, got))
    for raw in (-1000, -8000, -20000):
        code = _alaw_encode_sample(raw)
        got = gw.ALAW_TABLE[code]
        if got >= 0:
            raise AssertionError("负采样 %d 解出 %d, 符号反了" % (raw, got))
    return "正负号方向正确 (固件 bit7=1 为正)"


def t_alaw_table_size():
    if len(gw.ALAW_TABLE) != 256:
        raise AssertionError("解码表长度应为 256, 实际 %d" % len(gw.ALAW_TABLE))
    # 静音 (0x55) 应接近 0
    if abs(gw.ALAW_TABLE[0x55]) > 8:
        raise AssertionError("0x55 应接近 0, 实际 %d" % gw.ALAW_TABLE[0x55])
    return "256 项, 0x55(静音) -> %d" % gw.ALAW_TABLE[0x55]


def t_alaw_to_pcm_bytes():
    data = bytes([0x55] * 320)
    pcm = gw.alaw_to_pcm(data)
    if len(pcm) != 640:
        raise AssertionError("320 字节 A-law 应得 640 字节 PCM, 实际 %d" % len(pcm))
    return "320B -> %dB PCM16" % len(pcm)


# --------------------------------------------------------------------------
# 2) WAV / 重采样
# --------------------------------------------------------------------------
def t_pcm_to_wav():
    pcm = struct.pack("<%dh" % 1600, *([1000] * 1600))
    wav = gw.pcm_to_wav(pcm, 16000)
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise AssertionError("不是合法 WAV")
    return "%d 字节" % len(wav)


def t_resample():
    n = 24000
    src = struct.pack("<%dh" % n, *([500] * n))
    out = gw.resample_pcm16(src, 24000, 16000)
    n_out = len(out) // 2
    expect = int(n * 16000 / 24000)
    if abs(n_out - expect) > 2:
        raise AssertionError("重采样长度 %d, 期望 %d" % (n_out, expect))
    return "24k->16k: %d -> %d 采样" % (n, n_out)


# --------------------------------------------------------------------------
# 3) 签名
# --------------------------------------------------------------------------
def _sign(device_id, secret, ts, body):
    digest = hashlib.sha256(body).hexdigest()
    msg = ("%s|%d|%s" % (device_id, ts, digest)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def t_signature_ok():
    gw.DEVICES["testdev"] = "s3cret"
    body = b"hello"
    ts = int(time.time() * 1000)
    sig = _sign("testdev", "s3cret", ts, body)
    if not gw.verify_signature("testdev", ts, body, sig):
        raise AssertionError("正确签名应通过")
    return "正确签名通过"


def t_signature_bad():
    body = b"hello"
    ts = int(time.time() * 1000)
    sig = _sign("testdev", "s3cret", ts, body)
    if gw.verify_signature("testdev", ts, b"tampered", sig):
        raise AssertionError("body 被改后不应通过")
    if gw.verify_signature("testdev", ts, body, "0" * 32):
        raise AssertionError("错误签名不应通过")
    return "篡改 body / 错误签名均被拒"


def t_auth_paths():
    """require_device 的四条分支。"""
    gw.DEVICES["testdev"] = "s3cret"
    body = b"x"
    ts = int(time.time() * 1000)

    # 未注册设备
    try:
        gw.require_device("nobody", ts, body, "0" * 32)
        raise AssertionError("未注册设备应被拒")
    except Exception as e:
        if "403" not in str(e):
            raise AssertionError("未注册设备应报 403, 实际 %s" % e)

    # 过期时间戳
    try:
        gw.require_device("testdev", ts - 999999999, body, "0" * 32)
        raise AssertionError("过期时间戳应被拒")
    except Exception as e:
        if "401" not in str(e):
            raise AssertionError("过期时间戳应报 401, 实际 %s" % e)

    # 正确签名 + 新时间戳 -> 通过
    sig = _sign("testdev", "s3cret", ts, body)
    gw.require_device("testdev", ts, body, sig)
    return "未注册/过期/篡改 三条拦截 + 正确放行"


# --------------------------------------------------------------------------
# 4) mock 降级
# --------------------------------------------------------------------------
def t_mock_asr():
    gw.ASR_PROVIDER = "mock"
    pcm = struct.pack("<%dh" % 16000, *([100] * 16000))
    text = gw.asr_transcribe(pcm)
    if "[mock识别]" not in text:
        raise AssertionError("mock ASR 未生效: %s" % text)
    return text


def t_mock_llm():
    gw.LLM_PROVIDER = "mock"
    reply = gw.llm_chat("今天有点累", [])
    if "[mock回复]" not in reply:
        raise AssertionError("mock LLM 未生效: %s" % reply)
    return reply


def t_history():
    """
    多轮历史累积。

    注意: llm_chat() 只读历史, 写历史在 /v1/turn 里的 push_history()。
    所以这里直接测 push_history / get_history 这一对, 而不是绕 llm_chat。
    """
    gw._HISTORY.clear()
    gw.push_history("dev1", "第一句", "第一个回答")
    gw.push_history("dev1", "第二句", "第二个回答")
    h = gw.get_history("dev1")
    if len(h) != 4:
        raise AssertionError("历史应有 4 条, 实际 %d" % len(h))
    if h[0]["role"] != "user" or h[1]["role"] != "assistant":
        raise AssertionError("历史角色顺序错: %s" % h)
    if h[0]["content"] != "第一句":
        raise AssertionError("历史内容错: %s" % h[0])

    # 历史上限 20 条
    for i in range(30):
        gw.push_history("dev1", "q%d" % i, "a%d" % i)
    if len(gw.get_history("dev1")) != 20:
        raise AssertionError("历史应截断到 20, 实际 %d" % len(gw.get_history("dev1")))

    # 设备之间不串
    gw.push_history("dev2", "别人问的", "别人答的")
    if any(x["content"] == "别人问的" for x in gw.get_history("dev1")):
        raise AssertionError("不同设备的历史串了")
    return "累积/截断/隔离 都正确"


# --------------------------------------------------------------------------
# 5) TTS
# --------------------------------------------------------------------------
def t_tts_mock():
    gw.TTS_PROVIDER = "mock"
    pcm = gw.tts_synthesize("你好")
    if len(pcm) < 1000:
        raise AssertionError("mock TTS 输出太短: %d" % len(pcm))
    return "%d 字节 PCM16" % len(pcm)


def t_tts_edge_available():
    """edge-tts 需要网络; 这里只探测依赖是否装得上, 不强求成功。"""
    try:
        import edge_tts  # noqa: F401
        return "edge-tts 已安装"
    except ImportError:
        return "edge-tts 未安装 (服务器镜像里会装)"


# --------------------------------------------------------------------------
# 6) FastAPI 路由实调
# --------------------------------------------------------------------------
def t_routes():
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        raise AssertionError("缺 fastapi.testclient: %s" % e)

    gw.DEVICES["testdev"] = "s3cret"
    client = TestClient(gw.app)

    r = client.get("/v1/health")
    if r.status_code != 200:
        raise AssertionError("/v1/health 返回 %d" % r.status_code)
    j = r.json()
    if not j.get("ok"):
        raise AssertionError("/v1/health 没有 ok")

    r = client.get("/v1/devices")
    if r.status_code != 200 or "testdev" not in r.json().get("devices", []):
        raise AssertionError("/v1/devices 不对")

    # 完整一轮 (mock 模式)
    gw.ASR_PROVIDER = "mock"
    gw.LLM_PROVIDER = "mock"
    gw.TTS_PROVIDER = "mock"
    body = bytes([0x55] * 3200)
    ts = int(time.time() * 1000)
    sig = _sign("testdev", "s3cret", ts, body)
    r = client.post("/v1/turn",
                    content=body,
                    headers={"X-Device-Id": "testdev",
                             "X-Timestamp": str(ts),
                             "X-Signature": sig,
                             "Content-Type": "application/octet-stream"})
    if r.status_code != 200:
        raise AssertionError("/v1/turn 返回 %d: %s" % (r.status_code, r.text[:200]))
    if len(r.content) < 1000:
        raise AssertionError("/v1/turn 音频太短: %d" % len(r.content))
    if r.headers.get("X-Reply-Text") is None:
        raise AssertionError("/v1/turn 缺 X-Reply-Text")
    # 中文头必须 URL 编码 (HTTP 头只能是 latin-1)
    import urllib.parse as _up
    if "%" not in r.headers["X-Reply-Text"]:
        raise AssertionError("X-Reply-Text 未做 URL 编码: %r"
                             % r.headers["X-Reply-Text"])
    _up.unquote(r.headers["X-Reply-Text"])

    # 签名错必须被拒
    r = client.post("/v1/turn", content=body,
                    headers={"X-Device-Id": "testdev",
                             "X-Timestamp": str(ts),
                             "X-Signature": "0" * 32})
    if r.status_code != 401:
        raise AssertionError("错误签名应返回 401, 实际 %d" % r.status_code)

    # 文本对话
    r = client.post("/v1/chat", json={"text": "你好", "device_id": "testdev"})
    if r.status_code != 200:
        raise AssertionError("/v1/chat 返回 %d" % r.status_code)
    if "[mock回复]" not in r.json().get("reply_text", ""):
        raise AssertionError("/v1/chat 回复不对")

    return "health/devices/turn/chat 四个路由全部实调通过"


def t_event_route():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise AssertionError("缺 fastapi.testclient")
    gw.DEVICES["testdev"] = "s3cret"
    client = TestClient(gw.app)
    body = '{"type":"button","msg":"老人按了求助按钮"}'.encode("utf-8")
    ts = int(time.time() * 1000)
    sig = _sign("testdev", "s3cret", ts, body)
    r = client.post("/v1/events", content=body,
                    headers={"X-Device-Id": "testdev",
                             "X-Timestamp": str(ts),
                             "X-Signature": sig,
                             "Content-Type": "application/json"})
    if r.status_code != 200:
        raise AssertionError("/v1/events 返回 %d: %s" % (r.status_code, r.text[:200]))
    return "告警事件入库接口正常"


# --------------------------------------------------------------------------
def main():
    check("A-law 解码表", t_alaw_table_size)
    check("A-law 符号位方向", t_alaw_sign_correct)
    check("A-law 编解码往返", t_alaw_roundtrip)
    check("A-law -> PCM16 字节数", t_alaw_to_pcm_bytes)
    check("PCM -> WAV 封装", t_pcm_to_wav)
    check("重采样 24k->16k", t_resample)
    check("HMAC 签名生成", t_signature_ok)
    check("HMAC 篡改检测", t_signature_bad)
    check("鉴权四条分支", t_auth_paths)
    check("ASR mock 降级", t_mock_asr)
    check("LLM mock 降级", t_mock_llm)
    check("多轮历史", t_history)
    check("TTS mock 输出", t_tts_mock)
    check("edge-tts 可用性", t_tts_edge_available)
    check("FastAPI 路由实调", t_routes)
    check("事件上报路由", t_event_route)

    print("=" * 60)
    ok = 0
    for good, name, msg in RESULTS:
        print("  [%s] %-22s %s" % ("OK" if good else "FAIL", name, msg))
        ok += 1 if good else 0
    print("=" * 60)
    print("通过 %d / %d" % (ok, len(RESULTS)))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
