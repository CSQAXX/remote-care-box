"""
养老盒子 · AI 网关 (kanhu-ai-gateway)

把大模型能力放到服务器上, 让板子固件直连, 不依赖 PC 常开。

链路:
    板子麦克风 -> G.711 A-law 16k  -> POST /v1/turn
                -> [ASR] 云 ASR -> 文本
                -> [LLM] 云大模型 -> 回复文本 (带多轮上下文)
                -> [TTS] 云/免费 TTS -> PCM16 16k
                <- 返回 PCM16 16k 给板子喇叭播放

为什么是"网关"而不是把完整小智服务端搬上服务器:
    完整小智服务端要 MySQL + Redis + torch 2.2.2 + SenseVoice 权重,
    这台生产机只有 3.6GB 内存 / 7.4GB 剩余磁盘, 跑不动。
    所以这里只做【协议转换 + 鉴权 + 转发】, 模型全部走云 API,
    本地零权重, 内存占用 200MB 以内。

设计要点:
  - 纯 Python 标准库 + FastAPI/Uvicorn, 不用 torch
  - 独立数据库 kanhu, 与 jiuzuo 的 Mongo 完全分开
  - 设备鉴权用 HMAC-SHA256 (沿用固件 push_client 的签名风格)
  - ASR/LLM/TTS 三段各自可插拔, 缺 key 就降级, 不整条链路哑掉
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

# --------------------------------------------------------------------------
# 配置 (全部来自环境变量, 密钥不写进镜像)
# --------------------------------------------------------------------------
GATEWAY_HOST = os.environ.get("KANHU_HOST", "0.0.0.0")
GATEWAY_PORT = int(os.environ.get("KANHU_PORT", "18000"))

ASR_BASE_URL = os.environ.get("KANHU_ASR_BASE_URL", "https://api.siliconflow.cn/v1")
ASR_API_KEY = os.environ.get("KANHU_ASR_API_KEY", "")
ASR_MODEL = os.environ.get("KANHU_ASR_MODEL", "FunAudioLLM/SenseVoiceSmall")
# mock = 不调云 ASR, 直接把音频长度当成识别结果。用于没有 key 时验证整条链路。
ASR_PROVIDER = os.environ.get("KANHU_ASR_PROVIDER", "auto")   # auto | cloud | mock

LLM_BASE_URL = os.environ.get("KANHU_LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.environ.get("KANHU_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("KANHU_LLM_MODEL", "deepseek-chat")
LLM_TEMPERATURE = float(os.environ.get("KANHU_LLM_TEMPERATURE", "0.7"))
LLM_MAX_TOKENS = int(os.environ.get("KANHU_LLM_MAX_TOKENS", "160"))
# mock = 不调云 LLM, 返回固定文本。用于没有 key 时验证整条链路。
LLM_PROVIDER = os.environ.get("KANHU_LLM_PROVIDER", "auto")   # auto | cloud | mock

TTS_PROVIDER = os.environ.get("KANHU_TTS_PROVIDER", "edge")   # edge | openai | sapi | mock
TTS_BASE_URL = os.environ.get("KANHU_TTS_BASE_URL", "")
TTS_API_KEY = os.environ.get("KANHU_TTS_API_KEY", "")
TTS_MODEL = os.environ.get("KANHU_TTS_MODEL", "")
TTS_VOICE = os.environ.get("KANHU_TTS_VOICE", "zh-CN-XiaoxiaoNeural")

# 设备密钥表: device_id -> secret
# 格式 "device_id:secret,device_id2:secret2", 也支持用文件指定
DEVICES_ENV = os.environ.get("KANHU_DEVICES", "")
DEVICES_FILE = os.environ.get("KANHU_DEVICES_FILE", "/kanhu-ai/data/devices.txt")

MONGO_URI = os.environ.get("KANHU_MONGO_URI", "mongodb://127.0.0.1:27018")
MONGO_DB = os.environ.get("KANHU_MONGO_DB", "kanhu")

SAMPLE_RATE = 16000
MAX_AUDIO_SECONDS = 30

SYSTEM_PROMPT = (
    "你是「远程看护盒子」里的语音陪伴助手，正在陪一位独居老人聊天。"
    "说话要短、口语化、温和，一次回复不超过 60 个字，方便语音播出来听清。"
    "不要使用 Markdown、列表、括号说明或表情符号。"
    "不做医疗诊断；老人说身体不舒服、摔倒或需要帮助时，"
    "提醒他按盒子上的求助键，并告诉他已经通知家人。"
)

app = FastAPI(title="kanhu-ai-gateway", version="1.0.0")

_mongo_client = None
_mongo_db = None


# --------------------------------------------------------------------------
# G.711 A-law 解码 (板子上行格式)
#
# 为什么自己写表而不用 audioop:
#   Python 3.13 已移除 audioop, 而且网关跑在 Linux 容器里,
#   保证不依赖任何系统库最省事。
#
# A-law 编码规则 (ITU-T G.711):
#   7 位有效幅值, 1 位符号, 偶数位反转
# --------------------------------------------------------------------------
def _build_alaw_table() -> list[int]:
    """
    必须与固件 firmware/main/g711.c 的 g711_alaw_decode() 逐位对齐。

    固件原文:
        a_val = byte ^ 0x55;
        t     = (a_val & 0x0F) << 4;
        seg   = (a_val & 0x70) >> 4;
        seg==0 -> t += 8
        seg==1 -> t += 0x108
        else   -> t = (t + 0x108) << (seg - 1)
        return (a_val & 0x80) ? t : -t;     <- bit7=1 是【正】

    踩过的坑: 符号位一开始写反了 (以为 bit7=1 是负),
    结果整个语音听起来是反相的, 而且测试用「相对误差」判定没发现。
    所以这里严格照抄固件, 并在测试里用固件的编码逻辑做往返验证。
    """
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


ALAW_TABLE = _build_alaw_table()


def alaw_to_pcm(data: bytes) -> bytes:
    """G.711 A-law -> PCM16 little-endian 单声道。"""
    out = bytearray(len(data) * 2)
    for i, b in enumerate(data):
        s = ALAW_TABLE[b]
        struct.pack_into("<h", out, i * 2, s)
    return bytes(out)


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """PCM16 -> WAV (供只吃 wav 的 ASR 接口用)。"""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """线性插值重采样 (够用, TTS 出来 24k -> 板子要 16k)。"""
    if src_rate == dst_rate or not pcm:
        return pcm
    n_src = len(pcm) // 2
    if n_src == 0:
        return pcm
    src = struct.unpack("<%dh" % n_src, pcm)
    n_dst = int(n_src * dst_rate / src_rate)
    out = bytearray()
    for i in range(n_dst):
        x = i * src_rate / dst_rate
        i0 = int(x)
        i1 = min(i0 + 1, n_src - 1)
        frac = x - i0
        v = src[i0] * (1 - frac) + src[i1] * frac
        out += struct.pack("<h", int(max(-32768, min(32767, v))))
    return bytes(out)


# --------------------------------------------------------------------------
# HTTP 工具
# --------------------------------------------------------------------------
def http_json(url: str, payload: dict, headers: dict, timeout: int = 40) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError("HTTP %d: %s" % (e.code, detail)) from e
    except Exception as e:
        raise RuntimeError("请求失败: %s" % e) from e


def http_multipart(url: str, fields: dict, files: dict, headers: dict,
                   timeout: int = 40) -> dict:
    """multipart/form-data 上传 (ASR 接口用)。"""
    boundary = "----kanhu%s" % hashlib.md5(str(time.time()).encode()).hexdigest()
    body = bytearray()
    for k, v in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, k, v)).encode("utf-8")
    for k, (fname, content, ctype) in files.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                 "Content-Type: %s\r\n\r\n" % (boundary, k, fname, ctype)).encode("utf-8")
        body += content
        body += b"\r\n"
    body += ("--%s--\r\n" % boundary).encode("utf-8")

    h = dict(headers)
    h["Content-Type"] = "multipart/form-data; boundary=%s" % boundary
    req = urllib.request.Request(url, data=bytes(body), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError("HTTP %d: %s" % (e.code, detail)) from e
    except Exception as e:
        raise RuntimeError("请求失败: %s" % e) from e


# --------------------------------------------------------------------------
# 设备鉴权 (HMAC-SHA256)
# --------------------------------------------------------------------------
def _load_devices() -> dict[str, str]:
    devices: dict[str, str] = {}
    raw = DEVICES_ENV.strip()
    if raw:
        for item in raw.split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            did, sec = item.split(":", 1)
            devices[did.strip()] = sec.strip()
    if os.path.isfile(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or ":" not in line:
                        continue
                    did, sec = line.split(":", 1)
                    devices[did.strip()] = sec.strip()
        except Exception:
            pass
    return devices


DEVICES = _load_devices()


def _canonical_string(device_id: str, ts_ms: int, body: bytes) -> bytes:
    """签名原文: device_id + ts + sha256(body)。"""
    digest = hashlib.sha256(body).hexdigest()
    return ("%s|%d|%s" % (device_id, ts_ms, digest)).encode("utf-8")


def verify_signature(device_id: str, ts_ms: int, body: bytes, signature: str) -> bool:
    secret = DEVICES.get(device_id)
    if not secret:
        return False
    try:
        sig = int(signature, 16)
    except (TypeError, ValueError):
        return False
    expect = hmac.new(secret.encode("utf-8"),
                       _canonical_string(device_id, ts_ms, body),
                       hashlib.sha256).digest()
    return hmac.compare_digest(sig.to_bytes(16, "big"), expect[:16])


def require_device(device_id: str, ts_ms: int, body: bytes, signature: str) -> None:
    if not DEVICES:
        raise HTTPException(status_code=503,
                            detail="设备表为空, 网关未配置任何设备密钥")
    if device_id not in DEVICES:
        raise HTTPException(status_code=403, detail="未注册的设备")
    if ts_ms <= 0:
        raise HTTPException(status_code=400, detail="缺少时间戳")
    # 允许 10 分钟时钟偏差
    if abs(int(time.time() * 1000) - ts_ms) > 600000:
        raise HTTPException(status_code=401, detail="时间戳过期")
    if not verify_signature(device_id, ts_ms, body, signature):
        raise HTTPException(status_code=401, detail="签名校验失败")


# --------------------------------------------------------------------------
# 数据库 (独立库 kanhu, 与 jiuzuo 完全分开)
# --------------------------------------------------------------------------
def get_db():
    global _mongo_client, _mongo_db
    if _mongo_db is not None:
        return _mongo_db
    try:
        from pymongo import MongoClient
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        _mongo_client.admin.command("ping")
        _mongo_db = _mongo_client[MONGO_DB]
    except Exception as e:
        print("[warn] MongoDB 不可用 (%s), 事件将只落日志" % e)
        _mongo_db = None
    return _mongo_db


def save_event(device_id: str, event: dict) -> None:
    doc = dict(event)
    doc["device_id"] = device_id
    doc["received_at"] = int(time.time() * 1000)
    db = get_db()
    if db is None:
        print("[event] %s" % json.dumps(doc, ensure_ascii=False))
        return
    try:
        db.events.insert_one(doc)
    except Exception as e:
        print("[warn] 事件入库失败: %s" % e)


# --------------------------------------------------------------------------
# 三段能力: ASR / LLM / TTS
# --------------------------------------------------------------------------
def asr_transcribe(pcm: bytes) -> str:
    """PCM16 16k -> 中文文本。走云 ASR (硅基流动 SenseVoice)。"""
    provider = ASR_PROVIDER
    if provider == "auto":
        provider = "cloud" if ASR_API_KEY else "mock"

    if provider == "mock":
        # 没有 key 时的兜底: 用音频长度当"识别结果",
        # 这样整条链路 (编码/解码/播放) 仍能被完整验证。
        secs = len(pcm) // 2 / SAMPLE_RATE
        return "[mock识别] 收到 %.1f 秒语音" % secs

    if not ASR_API_KEY:
        raise RuntimeError("未配置 KANHU_ASR_API_KEY")
    wav = pcm_to_wav(pcm, SAMPLE_RATE)
    result = http_multipart(
        ASR_BASE_URL.rstrip("/") + "/audio/transcriptions",
        fields={"model": ASR_MODEL, "language": "zh", "response_format": "json"},
        files={"file": ("speech.wav", wav, "audio/wav")},
        headers={"Authorization": "Bearer " + ASR_API_KEY},
    )
    text = (result.get("text") or "").strip()
    if not text:
        raise RuntimeError("ASR 返回空文本: %s" % json.dumps(result, ensure_ascii=False)[:300])
    return text


def llm_chat(user_text: str, history: list[dict]) -> str:
    """用户文本 -> 回复文本 (带多轮上下文)。走云大模型。"""
    provider = LLM_PROVIDER
    if provider == "auto":
        provider = "cloud" if LLM_API_KEY else "mock"

    if provider == "mock":
        return ("[mock回复] 我听到了「%s」。这是没配 key 时的占位回复。"
                "配好 KANHU_LLM_API_KEY 后就是真的大模型在跟你说话。"
                % user_text[:30])

    if not LLM_API_KEY:
        raise RuntimeError("未配置 KANHU_LLM_API_KEY")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history[-10:]:
        role = h.get("role")
        content = h.get("content") or ""
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})

    result = http_json(
        LLM_BASE_URL.rstrip("/") + "/chat/completions",
        {"model": LLM_MODEL, "messages": messages,
         "temperature": LLM_TEMPERATURE, "max_tokens": LLM_MAX_TOKENS,
         "stream": False},
        {"Content-Type": "application/json",
         "Authorization": "Bearer " + LLM_API_KEY},
    )
    try:
        return result["choices"][0]["message"]["content"].strip()
    except Exception:
        raise RuntimeError("LLM 返回结构异常: %s" % json.dumps(result, ensure_ascii=False)[:300])


def tts_synthesize(text: str) -> bytes:
    """文本 -> PCM16 16k。edge-tts 免费, 失败自动降级到 sapi / mock。"""
    if TTS_PROVIDER == "openai":
        return _tts_openai(text)
    if TTS_PROVIDER == "sapi":
        return _tts_sapi(text)
    if TTS_PROVIDER == "mock":
        return _tts_mock(text)
    try:
        return _tts_edge(text)
    except Exception as e:
        print("[warn] edge-tts 失败 (%s), 降级 sapi" % e)
        try:
            return _tts_sapi(text)
        except Exception as e2:
            print("[warn] sapi 也失败 (%s), 降级 mock" % e2)
            return _tts_mock(text)


def _tts_edge(text: str) -> bytes:
    import asyncio

    import edge_tts

    async def run():
        comm = edge_tts.Communicate(text, TTS_VOICE)
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    mp3 = asyncio.run(run())
    if not mp3:
        raise RuntimeError("edge-tts 返回空音频")
    return _decode_audio_bytes(mp3)


def _tts_openai(text: str) -> bytes:
    if not TTS_API_KEY or not TTS_BASE_URL:
        raise RuntimeError("未配置 KANHU_TTS_BASE_URL / KANHU_TTS_API_KEY")
    result = http_json(
        TTS_BASE_URL.rstrip("/") + "/audio/speech",
        {"model": TTS_MODEL or "tts-1", "voice": TTS_VOICE or "alloy",
         "input": text, "response_format": "wav"},
        {"Content-Type": "application/json",
         "Authorization": "Bearer " + TTS_API_KEY},
    )
    return result if isinstance(result, bytes) else b""


def _tts_sapi(text: str) -> bytes:
    import subprocess
    import tempfile
    base64_mod = base64
    tmp = tempfile.mkdtemp(prefix="kanhu_tts_")
    txt = os.path.join(tmp, "text.txt")
    wav = os.path.join(tmp, "out.wav")
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text)
    script = (
        'Add-Type -AssemblyName System.Speech\n'
        '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n'
        '$s.SetOutputToWaveFile("' + wav + '")\n'
        '$s.Speak([System.IO.File]::ReadAllText("' + txt +
        '", [System.Text.Encoding]::UTF8))\n'
        '$s.Dispose()\n'
    )
    enc = base64_mod.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.run(["powershell", "-NonInteractive", "-EncodedCommand", enc],
                   capture_output=True, timeout=60)
    if not os.path.isfile(wav):
        raise RuntimeError("sapi 没生成 wav (容器里没 Windows, 用 edge-tts 或云 TTS)")
    with wave.open(wav, "rb") as w:
        pcm = w.readframes(w.getnframes())
        rate = w.getframerate()
        ch = w.getnchannels()
    os.unlink(wav)
    os.unlink(txt)
    os.rmdir(tmp)
    if ch > 1:
        pcm = pcm[::ch * 2]
    return resample_pcm16(pcm, rate, SAMPLE_RATE)


def _tts_mock(text: str) -> bytes:
    """兜底: 出一段 440Hz 测试音, 至少证明下行是通的。"""
    dur = min(2.0, max(0.5, len(text) * 0.08))
    n = int(SAMPLE_RATE * dur)
    out = bytearray()
    for i in range(n):
        v = int(8000 * 0.4 * (1 if (i // 2000) % 2 == 0 else -1))
        out += struct.pack("<h", v)
    return bytes(out)


def _decode_audio_bytes(data: bytes) -> bytes:
    """把任意音频字节 (wav/mp3) 转成 PCM16 16k。"""
    # WAV 直接解析
    if data[:4] == b"RIFF":
        import io
        with wave.open(io.BytesIO(data), "rb") as w:
            pcm = w.readframes(w.getnframes())
            rate = w.getframerate()
            ch = w.getnchannels()
            sw = w.getsampwidth()
        if sw != 2:
            raise RuntimeError("只支持 16bit WAV")
        if ch > 1:
            pcm = pcm[::ch * 2]
        return resample_pcm16(pcm, rate, SAMPLE_RATE)

    # MP3: 优先 pydub/ffmpeg
    try:
        import io

        from pydub import AudioSegment
        seg = AudioSegment.from_file(io.BytesIO(data), format="mp3")
        seg = seg.set_frame_rate(SAMPLE_RATE).set_channels(1).set_sample_width(2)
        return seg.raw_data
    except Exception:
        pass
    try:
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(data)
            mp3_path = f.name
        wav_path = mp3_path + ".wav"
        subprocess.run(["ffmpeg", "-y", "-i", mp3_path, "-ar", str(SAMPLE_RATE),
                        "-ac", "1", "-sample_fmt", "s16", wav_path],
                       capture_output=True, timeout=60)
        if not os.path.isfile(wav_path):
            raise RuntimeError("ffmpeg 没产出 wav")
        with wave.open(wav_path, "rb") as w:
            pcm = w.readframes(w.getnframes())
        os.unlink(mp3_path)
        os.unlink(wav_path)
        return pcm
    except Exception as e:
        raise RuntimeError("无法解码音频: %s" % e) from e


# --------------------------------------------------------------------------
# 会话历史 (进程内内存, 按 device_id 分组)
# --------------------------------------------------------------------------
_HISTORY: dict[str, list[dict]] = {}
_HISTORY_LOCK = __import__("threading").Lock()


def get_history(device_id: str) -> list[dict]:
    with _HISTORY_LOCK:
        return list(_HISTORY.get(device_id, []))


def push_history(device_id: str, user_text: str, reply: str) -> None:
    with _HISTORY_LOCK:
        h = _HISTORY.setdefault(device_id, [])
        h.append({"role": "user", "content": user_text})
        h.append({"role": "assistant", "content": reply})
        del h[:-20]


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.get("/v1/health")
def health():
    db = get_db()
    return {
        "ok": True,
        "service": "kanhu-ai-gateway",
        "uptime": int(time.time()),
        "devices": len(DEVICES),
        "mongo": bool(db),
        "asr": {"configured": bool(ASR_API_KEY), "model": ASR_MODEL},
        "llm": {"configured": bool(LLM_API_KEY), "model": LLM_MODEL},
        "tts": {"provider": TTS_PROVIDER, "voice": TTS_VOICE},
    }


@app.get("/v1/devices")
def devices():
    """给运维看: 已注册哪些设备 (不返回密钥)。"""
    return {"devices": sorted(DEVICES.keys())}


@app.post("/v1/turn")
async def turn(request: Request,
               x_device_id: str = Header(default=""),
               x_timestamp: str = Header(default=""),
               x_signature: str = Header(default="")):
    """
    完整一轮对话: 音频进 -> 文本回复 + 语音出。

    请求体: 原始 G.711 A-law 字节 (Content-Type: application/octet-stream)
    响应体: 原始 PCM16 16k 单声道 (Content-Type: audio/L16)
    响应头 X-User-Text / X-Reply-Text 带识别和回复文本, 方便板子显示到屏幕
    """
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="空请求体")
    try:
        ts = int(x_timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="缺少 X-Timestamp")

    require_device(x_device_id, ts, body, x_signature)

    pcm = alaw_to_pcm(body)
    max_bytes = MAX_AUDIO_SECONDS * SAMPLE_RATE * 2
    if len(pcm) > max_bytes:
        pcm = pcm[:max_bytes]

    try:
        user_text = asr_transcribe(pcm)
    except Exception as e:
        raise HTTPException(status_code=502, detail="ASR 失败: %s" % e)

    try:
        reply = llm_chat(user_text, get_history(x_device_id))
    except Exception as e:
        raise HTTPException(status_code=502, detail="LLM 失败: %s" % e)

    try:
        out_pcm = tts_synthesize(reply)
    except Exception as e:
        raise HTTPException(status_code=502, detail="TTS 失败: %s" % e)

    push_history(x_device_id, user_text, reply)
    save_event(x_device_id, {"type": "ai_turn", "user_text": user_text,
                              "reply_text": reply, "ts": ts})

    # HTTP 头只能是 latin-1, 中文必须 URL 编码后再放。
    # 板子侧解码时再做一次 unquote。
    return Response(
        content=out_pcm,
        media_type="audio/L16",
        headers={
            "X-Rate": str(SAMPLE_RATE),
            "X-Channels": "1",
            "X-User-Text": urllib.parse.quote(user_text),
            "X-Reply-Text": urllib.parse.quote(reply),
            "X-Device-Id": x_device_id,
        },
    )


@app.post("/v1/chat")
async def chat(request: Request):
    """纯文本对话 (不经过 ASR/TTS), 用来在服务器上快速验证 LLM。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="缺少 text")
    device_id = payload.get("device_id") or "console"
    try:
        reply = llm_chat(text, get_history(device_id))
    except Exception as e:
        raise HTTPException(status_code=502, detail="LLM 失败: %s" % e)
    push_history(device_id, text, reply)
    return {"ok": True, "user_text": text, "reply_text": reply}


@app.post("/v1/events")
async def events(request: Request,
                 x_device_id: str = Header(default=""),
                 x_timestamp: str = Header(default=""),
                 x_signature: str = Header(default="")):
    """设备告警事件上报 (求助按键 / 断电 / 活动异常), 入独立库。"""
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="空请求体")
    try:
        ts = int(x_timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="缺少 X-Timestamp")
    require_device(x_device_id, ts, body, x_signature)
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="事件体不是 JSON")
    save_event(x_device_id, payload)
    return {"ok": True, "stored": True}


@app.get("/v1/events")
def list_events(limit: int = 50):
    """运维查询: 最近的事件。"""
    db = get_db()
    if db is None:
        return {"ok": False, "error": "MongoDB 不可用", "events": []}
    try:
        cur = db.events.find().sort("received_at", -1).limit(min(limit, 200))
        out = []
        for doc in cur:
            doc.pop("_id", None)
            out.append(doc)
        return {"ok": True, "events": out}
    except Exception as e:
        return {"ok": False, "error": str(e), "events": []}


@app.get("/")
def root():
    return {"service": "kanhu-ai-gateway", "docs": "/docs", "health": "/v1/health"}
