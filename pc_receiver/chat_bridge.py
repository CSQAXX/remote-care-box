#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
远程看护盒子 · 语言大模型对话桥

在【不改动 ESP32 固件】的前提下，让盒子具备语音对话能力。

链路：
    板子麦克风 -> GET /audio/alaw (G.711 A-law 16k 长连接)
              -> A-law 解码 -> VAD 断句
              -> ASR -> LLM(多轮) -> TTS
              -> PCM16 16k -> POST /audio/down -> 板子喇叭

两种「大脑」后端，配置里用 backend 切换：
    openai   —— ASR / LLM / TTS 各自走 OpenAI 兼容 HTTP 接口（DeepSeek、
                硅基流动、通义、豆包、本地 Ollama 都能接）。开箱即用。
    xiaozhi  —— 走小智(xiaozhi-esp32-server)的 WebSocket 协议，把 PC 端
                模拟成一台小智设备。服务端负责 ASR/LLM/TTS/VAD/MCP。
                这就是「32 自带的小智」的正解：能力一样，但保住了自研固件。
                可选依赖：pip install websockets opuslib（opuslib 还需 libopus）

设计上刻意只依赖 Python 标准库；websockets / opuslib / edge-tts 都是
按需惰性导入，缺了只影响对应后端，不影响原有的画面与对讲功能。

命令行：
    python chat_bridge.py --selftest                # 不接板子，纯本地跑全链路自检
    python chat_bridge.py --device 192.168.2.138    # 直接跑语音对话（无 Web UI）
    python chat_bridge.py --device 192.168.2.138 --say "今天感觉怎么样"
                                                    # 文字问一句，绕开 ASR 调试
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "chat_config.json")
EXAMPLE_PATH = os.path.join(HERE, "chat_config.example.json")

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000      # 320
FRAME_BYTES = FRAME_SAMPLES * 2                     # 640

DEFAULT_SYSTEM_PROMPT = (
    "你是「远程看护盒子」里的语音陪伴助手，正在陪一位独居老人聊天。"
    "说话要短、口语化、温和，一次回复不超过 60 个字，方便语音播出来听清。"
    "不要使用 Markdown、列表、括号说明或表情符号。"
    "不做医疗诊断；老人说身体不舒服、摔倒或需要帮助时，提醒他按盒子上的求助键，"
    "并告诉他已经通知家人。"
)

DEFAULT_CONFIG = {
    "backend": "openai",          # openai | xiaozhi
    "device": "",                 # 留空 = 用 pc_console 的 --device
    "control_port": 8081,         # 控制面：/audio/alaw 与 /audio/down 都在这里
    "vad": {
        "energy_threshold": 350,  # 20ms 帧的 rms 门限（实测板上静音 ~21，人声 ~1000）
        "start_frames": 3,        # 连续几帧有声才认起句，抗突发噪声
        "silence_ms": 700,        # 连续静音多久算一句话说完
        "min_speech_ms": 400,     # 短于这个直接丢弃（咳嗽、碰撞）
        "max_speech_ms": 12000,   # 超时强制断句，防一句话把链路堵死
        "preroll_ms": 300         # 起句前留一段，避免吃掉第一个字
    },
    "asr": {
        "provider": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "model": "FunAudioLLM/SenseVoiceSmall",
        "language": "zh",
        "timeout": 30
    },
    "llm": {
        "provider": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "max_tokens": 160,
        "temperature": 0.7,
        "history_turns": 6,
        "timeout": 40
    },
    "tts": {
        "provider": "auto",       # auto | openai | edge | sapi | mock
        "base_url": "",
        "api_key": "",
        "model": "",
        "voice": "zh-CN-XiaoxiaoNeural",
        "response_format": "wav",
        "timeout": 40
    },
    "xiaozhi": {
        "ota_url": "",            # 自建服务端可填，响应里自动找 websocket.url / token
        "ws_url": "",            # 也可直接写 ws://host:8000/xiaozhi/v1/
        "token": "",
        "device_id": "",         # 一般用板子 MAC
        "client_id": "",         # 任意 UUID
        "downlink_rate": 24000,  # 服务端 hello 里的采样率会覆盖它
        "timeout": 40
    },
    "down": {
        "max_seconds": 20,       # 单次回复最长播放秒数
        "timeout": 90            # /audio/down 会按真实播放速率返回，超时要留足
    }
}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    """读 chat_config.json；不存在时退回内置默认值（key 为空）。"""
    path = path or CONFIG_PATH
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return _merge(DEFAULT_CONFIG, json.load(f))
        except Exception as e:
            print(f"[warn] 读配置失败({e})，改用内置默认值", file=sys.stderr)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def write_example(path=None):
    path = path or EXAMPLE_PATH
    with open(path, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    return path


def _env(key, default=""):
    return os.environ.get(key, default)


def apply_env(cfg):
    """环境变量优先级最高，方便不把 key 写进文件。"""
    cfg = json.loads(json.dumps(cfg))
    m = {
        "CHAT_BACKEND": ("backend", None),
        "LLM_API_KEY": ("llm", "api_key"),
        "LLM_BASE_URL": ("llm", "base_url"),
        "LLM_MODEL": ("llm", "model"),
        "ASR_API_KEY": ("asr", "api_key"),
        "ASR_BASE_URL": ("asr", "base_url"),
        "ASR_MODEL": ("asr", "model"),
        "TTS_API_KEY": ("tts", "api_key"),
        "TTS_BASE_URL": ("tts", "base_url"),
        "TTS_MODEL": ("tts", "model"),
        "XIAOZHI_WS_URL": ("xiaozhi", "ws_url"),
        "XIAOZHI_TOKEN": ("xiaozhi", "token"),
    }
    for env_key, (section, field) in m.items():
        val = _env(env_key)
        if not val:
            continue
        if field is None:
            cfg[section] = val
        else:
            cfg[section][field] = val
    return cfg


# ---------------------------------------------------------------------------
# 音频工具（纯标准库，不依赖 audioop，Python 3.13+ 也能跑）
# ---------------------------------------------------------------------------
def _alaw_decode_byte(b):
    """与 firmware/main/g711.c、g711_algo.py 逐行一致的 A-law 解码。"""
    a = b ^ 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= (seg - 1)
    v = t if (a & 0x80) else -t
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


_ALAW_BYTES = tuple(struct.pack("<h", _alaw_decode_byte(i)) for i in range(256))


def alaw_to_pcm(data: bytes) -> bytes:
    """G.711 A-law 裸流 -> PCM16 little-endian。"""
    return b"".join(_ALAW_BYTES[b] for b in data)


def pcm_rms(frame: bytes) -> float:
    n = len(frame) // 2
    if n <= 0:
        return 0.0
    vals = struct.unpack("<%dh" % n, frame[:n * 2])
    return math.sqrt(sum(v * v for v in vals) / n)


def pcm_to_wav(pcm: bytes, rate=SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def resample_pcm(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """线性插值重采样。TTS / 小智下行常是 24k，板子只吃 16k。"""
    if src_rate == dst_rate or not pcm:
        return pcm
    n = len(pcm) // 2
    vals = struct.unpack("<%dh" % n, pcm[:n * 2])
    out_n = max(1, int(n * dst_rate / src_rate))
    out = []
    for i in range(out_n):
        pos = i * src_rate / dst_rate
        i0 = int(pos)
        i1 = min(i0 + 1, n - 1)
        f = pos - i0
        v = vals[i0] * (1.0 - f) + vals[i1] * f
        out.append(int(max(-32768, min(32767, v))))
    return struct.pack("<%dh" % len(out), *out)


def normalize_pcm(pcm: bytes, rate: int, channels: int, sampwidth: int) -> bytes:
    """任意 WAV 参数 -> PCM16 16k 单声道。"""
    if sampwidth == 1:                       # 8bit 无符号
        pcm = b"".join(struct.pack("<h", (b - 128) * 256) for b in pcm)
        sampwidth = 2
    if sampwidth != 2:
        raise ValueError("只支持 8/16 bit PCM，其它格式请配 ffmpeg")
    if channels == 2:
        n = len(pcm) // 4
        vals = struct.unpack("<%dh" % (n * 2), pcm[:n * 4])
        mono = [int((vals[i * 2] + vals[i * 2 + 1]) / 2) for i in range(n)]
        pcm = struct.pack("<%dh" % n, *mono)
    elif channels != 1:
        raise ValueError("只支持单声道/双声道")
    return resample_pcm(pcm, rate, SAMPLE_RATE)


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
        if sys.platform == "win32" and os.path.isfile(p + ".exe"):
            return p + ".exe"
    return None


def audio_bytes_to_pcm(data: bytes) -> bytes:
    """TTS 返回的音频字节 -> PCM16 16k。先按 WAV 解，解不开再找 ffmpeg。"""
    if not data:
        raise ValueError("TTS 返回空音频")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return normalize_pcm(w.readframes(w.getnframes()),
                                 w.getframerate(), w.getnchannels(),
                                 w.getsampwidth())
    except Exception:
        pass
    ffmpeg = _which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("TTS 返回的不是 WAV，且本机没有 ffmpeg（装 ffmpeg 或改用返回 wav 的 TTS）")
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le",
         "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("ffmpeg 解码 TTS 失败: " + proc.stderr.decode("utf-8", "replace")[-300:])
    return proc.stdout


def tone_pcm(seconds=1.0, freq=440.0, amplitude=8000) -> bytes:
    """自检/兜底用：生成一段正弦提示音，至少能确认 /audio/down 通不通。"""
    n = int(SAMPLE_RATE * seconds)
    return struct.pack("<%dh" % n, *[
        int(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        for i in range(n)])


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------
def http_request(url, data=None, headers=None, method=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, payload, api_key="", timeout=30):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    raw = http_request(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       headers, "POST", timeout)
    return json.loads(raw.decode("utf-8", "replace"))


def http_multipart(url, fields, file_field, filename, content_type, filedata,
                   api_key="", timeout=30):
    boundary = "----rcb" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(("--" + boundary + "\r\n").encode())
        parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode())
        parts.append(("%s\r\n" % v).encode())
    parts.append(("--" + boundary + "\r\n").encode())
    parts.append(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                  % (file_field, filename)).encode())
    parts.append(("Content-Type: %s\r\n\r\n" % content_type).encode())
    parts.append(filedata)
    parts.append(("\r\n--" + boundary + "--\r\n").encode())
    body = b"".join(parts)
    headers = {"Content-Type": "multipart/form-data; boundary=" + boundary}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    raw = http_request(url, body, headers, "POST", timeout)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"text": raw.decode("utf-8", "replace")}


def post_down(device, port, pcm, timeout=90):
    """下行注入。注意固件按真实播放速率返回，请求会持续整段音频时长。"""
    url = "http://%s:%d/audio/down" % (device, port)
    return http_request(url, pcm, {"Content-Type": "application/octet-stream"},
                        "POST", timeout)


# ---------------------------------------------------------------------------
# VAD：能量 + 静音断句
# ---------------------------------------------------------------------------
class VAD:
    def __init__(self, cfg=None):
        c = cfg or DEFAULT_CONFIG["vad"]
        self.threshold = float(c.get("energy_threshold", 350))
        self.start_frames = int(c.get("start_frames", 3))
        self.silence_frames = max(1, int(c.get("silence_ms", 700)) // FRAME_MS)
        self.min_frames = max(1, int(c.get("min_speech_ms", 400)) // FRAME_MS)
        self.max_frames = max(self.min_frames,
                              int(c.get("max_speech_ms", 12000)) // FRAME_MS)
        self.preroll_frames = int(c.get("preroll_ms", 300)) // FRAME_MS
        self.reset()

    def reset(self):
        self._buf = bytearray()
        self._preroll = []
        self._speech = []
        self._voiced = 0
        self._silent = 0
        self._in_speech = False
        self._nframes = 0

    def feed(self, chunk: bytes):
        """喂任意长度 PCM16，返回完整一句（bytes）或 None。尾帧不足 20ms 留到下次。"""
        self._buf.extend(chunk)
        out = None
        while len(self._buf) >= FRAME_BYTES:
            frame = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            got = self._feed_frame(frame)
            if got:
                out = got
                break
        return out

    def _feed_frame(self, frame):
        loud = pcm_rms(frame) >= self.threshold
        if not self._in_speech:
            self._preroll.append(frame)
            if len(self._preroll) > self.preroll_frames:
                self._preroll.pop(0)
            self._voiced = self._voiced + 1 if loud else 0
            if self._voiced >= self.start_frames:
                self._in_speech = True
                self._speech = list(self._preroll)
                self._nframes = len(self._preroll)
                self._silent = 0
            return None

        self._speech.append(frame)
        self._nframes += 1
        self._silent = self._silent + 1 if not loud else 0
        if self._silent >= self.silence_frames or self._nframes >= self.max_frames:
            # 裁掉句尾静音
            if self._silent:
                self._speech = self._speech[:-self._silent]
                self._nframes -= self._silent
            utterance = b"".join(self._speech)
            # 必须先判足够长再 reset —— reset 会把 _nframes 清零，
            # 顺序反了的话每一句都会被当成「太短」丢掉。
            enough = self._nframes >= self.min_frames
            self.reset()
            return utterance if enough else None
        return None


# ---------------------------------------------------------------------------
# ASR / LLM / TTS
# ---------------------------------------------------------------------------
class OpenAIASR:
    def __init__(self, cfg):
        self.c = cfg

    def ready(self):
        return bool(self.c.get("api_key") and self.c.get("base_url"))

    def transcribe(self, pcm):
        url = self.c["base_url"].rstrip("/") + "/audio/transcriptions"
        fields = {"model": self.c.get("model", ""),
                  "language": self.c.get("language", "zh"),
                  "response_format": "json"}
        r = http_multipart(url, fields, "file", "speech.wav", "audio/wav",
                           pcm_to_wav(pcm), self.c.get("api_key", ""),
                           self.c.get("timeout", 30))
        text = r.get("text") or r.get("result") or ""
        if isinstance(text, list):
            text = " ".join(str(x) for x in text)
        return str(text).strip()


class OpenAILLM:
    def __init__(self, cfg):
        self.c = cfg
        self.history = []
        self.lock = threading.Lock()

    def ready(self):
        return bool(self.c.get("api_key") and self.c.get("base_url"))

    def clear(self):
        with self.lock:
            self.history = []

    def chat(self, text):
        url = self.c["base_url"].rstrip("/") + "/chat/completions"
        with self.lock:
            messages = [{"role": "system",
                         "content": self.c.get("system_prompt", DEFAULT_SYSTEM_PROMPT)}]
            messages += list(self.history)
            messages.append({"role": "user", "content": text})
        payload = {"model": self.c.get("model", "deepseek-chat"),
                   "messages": messages,
                   "max_tokens": int(self.c.get("max_tokens", 160)),
                   "temperature": float(self.c.get("temperature", 0.7)),
                   "stream": False}
        r = http_json(url, payload, self.c.get("api_key", ""),
                      self.c.get("timeout", 40))
        try:
            reply = r["choices"][0]["message"]["content"].strip()
        except Exception:
            raise RuntimeError("LLM 返回异常: " + json.dumps(r, ensure_ascii=False)[:300])
        turns = int(self.c.get("history_turns", 6))
        with self.lock:
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": reply})
            if turns > 0:
                self.history = self.history[-turns * 2:]
        return reply


class OpenAITTS:
    def __init__(self, cfg):
        self.c = cfg

    def ready(self):
        return bool(self.c.get("api_key") and self.c.get("base_url") and self.c.get("model"))

    def synth(self, text):
        url = self.c["base_url"].rstrip("/") + "/audio/speech"
        payload = {"model": self.c.get("model"),
                   "voice": self.c.get("voice", "alloy"),
                   "input": text,
                   "response_format": self.c.get("response_format", "wav")}
        data = http_request(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            {"Content-Type": "application/json",
                             "Authorization": "Bearer " + self.c.get("api_key", "")},
                            "POST", self.c.get("timeout", 40))
        return audio_bytes_to_pcm(data)


class EdgeTTS:
    """edge-tts 免费、无需 key，但返回 mp3，需要本机 ffmpeg 解码。"""
    def __init__(self, cfg):
        self.c = cfg

    def ready(self):
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return False
        return bool(_which("ffmpeg"))

    def synth(self, text):
        import asyncio
        import edge_tts

        async def run():
            comm = edge_tts.Communicate(text, self.c.get("voice", "zh-CN-XiaoxiaoNeural"))
            buf = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        return audio_bytes_to_pcm(asyncio.run(run()))


class SapiTTS:
    """
    Windows 自带语音合成，零依赖、零 key，中文 Windows 一般有中文语音。
    用 -EncodedCommand 传脚本，避开 PowerShell 的中文编码坑。
    """
    def __init__(self, cfg):
        self.c = cfg

    def ready(self):
        return sys.platform == "win32" and bool(_which("powershell") or _which("powershell.exe"))

    def synth(self, text):
        tmp = tempfile.mkdtemp(prefix="rcb_tts_")
        txt = os.path.join(tmp, "text.txt")
        wav_path = os.path.join(tmp, "out.wav")
        with open(txt, "w", encoding="utf-8") as f:
            f.write(text)
        # PowerShell 的转义符是反引号 `，不是反斜杠 —— 路径里的 \ 必须原样给，
        # 写成 \\ 反而会变成两个字面反斜杠导致找不到文件。
        script = (
            'Add-Type -AssemblyName System.Speech\n'
            '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n'
            '$s.SetOutputToWaveFile("' + wav_path + '")\n'
            '$s.Speak([System.IO.File]::ReadAllText("' + txt +
            '", [System.Text.Encoding]::UTF8))\n'
            '$s.Dispose()\n'
        )
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            subprocess.run(["powershell", "-NonInteractive", "-EncodedCommand", enc],
                           capture_output=True, timeout=60)
            if not os.path.isfile(wav_path):
                raise RuntimeError("SAPI 没生成 wav（可能没装中文语音包）")
            with wave.open(wav_path, "rb") as w:
                return normalize_pcm(w.readframes(w.getnframes()), w.getframerate(),
                                     w.getnchannels(), w.getsampwidth())
        finally:
            for p in (txt, wav_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            try:
                os.rmdir(tmp)
            except OSError:
                pass


class MockTTS:
    """自检 / 全链路兜底：至少让板子出个声，证明下行是通的。"""
    def ready(self):
        return True

    def synth(self, text):
        return tone_pcm(0.6)


class TTSChain:
    """按 provider 顺序尝试；auto 时自动降级，绝不因为没 key 就整条链路哑掉。"""
    def __init__(self, cfg):
        self.c = cfg
        want = str(cfg.get("provider", "auto")).lower()
        all_impl = {"openai": OpenAITTS(cfg), "edge": EdgeTTS(cfg),
                    "sapi": SapiTTS(cfg), "mock": MockTTS()}
        if want == "auto":
            order = ["openai", "edge", "sapi", "mock"]
        elif want in all_impl:
            order = [want] if want == "mock" else [want, "sapi", "mock"]
        else:
            order = ["openai", "edge", "sapi", "mock"]
        self.chain = [all_impl[k] for k in order]
        self.last_provider = None

    def synth(self, text):
        errors = []
        for impl in self.chain:
            name = type(impl).__name__
            try:
                if not impl.ready():
                    errors.append(name + ": 未配置/缺依赖")
                    continue
                pcm = impl.synth(text)
                if not pcm:
                    raise RuntimeError("返回空音频")
                self.last_provider = name
                if errors:
                    print("[chat][tts] 已降级到 %s，前序原因: %s" %
                          (name, " | ".join(errors)), file=sys.stderr)
                return pcm
            except Exception as e:
                errors.append(name + ": " + str(e)[:120])
        raise RuntimeError("所有 TTS 都失败 -> " + " | ".join(errors))


# ---------------------------------------------------------------------------
# 大脑：OpenAI 兼容三段式 / 小智 WebSocket
# ---------------------------------------------------------------------------
class OpenAIBrain:
    name = "openai"

    def __init__(self, cfg):
        self.asr = OpenAIASR(cfg["asr"])
        self.llm = OpenAILLM(cfg["llm"])
        self.tts = TTSChain(cfg["tts"])

    def check(self):
        problems = []
        if not self.asr.ready():
            problems.append("ASR 没配 api_key（chat_config.json -> asr.api_key）")
        if not self.llm.ready():
            problems.append("LLM 没配 api_key（chat_config.json -> llm.api_key）")
        return problems

    def reply(self, pcm, user_text=None):
        """user_text 给了就跳过 ASR（文字调试用）。"""
        heard = user_text or self.asr.transcribe(pcm)
        if not heard:
            return {"user_text": "", "reply_text": "", "pcm": b"", "skip": True}
        answer = self.llm.chat(heard)
        return {"user_text": heard, "reply_text": answer,
                "pcm": self.tts.synth(answer), "skip": False,
                "tts": self.tts.last_provider}

    def say(self, text):
        return self.reply(b"", user_text=text)


class XiaozhiBrain:
    """
    PC 端模拟一台小智设备，连 xiaozhi-esp32-server 的 WebSocket。

    协议（78/xiaozhi-esp32 docs/websocket.md + xiaozhi.me 官方通信文档）：
      headers : Authorization: Bearer <token> / Protocol-Version: 1 /
                Device-Id: <MAC> / Client-Id: <UUID>
      hello   : {"type":"hello","version":1,"transport":"websocket",
                 "audio_params":{"format":"opus","sample_rate":16000,
                                 "channels":1,"frame_duration":60}}
      listen  : start(mode=manual) / stop
      binary  : v1 = 裸 Opus 帧（60ms），上下行同帧格式
      JSON    : stt / tts(start|stop|sentence_start) / llm / iot

    上行 16k Opus，下行服务端常见 24k，解码后重采样到 16k 再 POST 给板子。
    """
    name = "xiaozhi"

    def __init__(self, cfg):
        self.c = cfg["xiaozhi"]
        self.device_id = self.c.get("device_id") or "aa:bb:cc:dd:ee:ff"
        self.client_id = self.c.get("client_id") or str(uuid.uuid4())
        self.frame_samples = SAMPLE_RATE * 60 // 1000     # 960

    def check(self):
        problems = []
        try:
            import websockets  # noqa: F401
        except Exception:
            problems.append("缺 websockets：pip install websockets")
        try:
            import opuslib  # noqa: F401
        except Exception:
            # Windows 上 opuslib 装了但找不到 libopus DLL 会抛 OSError，不是 ImportError
            problems.append("缺 opuslib / libopus：pip install opuslib，并放好 libopus DLL")
        if not (self.c.get("ws_url") or self.c.get("ota_url")):
            problems.append("没填 xiaozhi.ws_url / ota_url（自建服务端地址）")
        return problems

    def _resolve(self):
        """没直接给 ws_url 时，走 OTA 接口问服务端要地址和 token。"""
        ws_url, token = self.c.get("ws_url", ""), self.c.get("token", "")
        if ws_url:
            return ws_url, token
        ota = self.c.get("ota_url", "")
        if not ota:
            return "", ""
        payload = {"device_id": self.device_id, "client_id": self.client_id,
                   "application": {"name": "remote-care-box", "version": "1.0.0"}}
        try:
            r = json.loads(http_request(
                ota, json.dumps(payload).encode("utf-8"),
                {"Content-Type": "application/json",
                 "Device-Id": self.device_id, "Client-Id": self.client_id},
                "POST", 15).decode("utf-8", "replace"))
            ws = r.get("websocket") or r.get("ws") or {}
            ws_url = ws.get("url") or r.get("ws_url") or ""
            token = ws.get("token") or r.get("token") or token
        except Exception as e:
            raise RuntimeError("小智 OTA 取 ws_url 失败: %s" % e)
        return ws_url, token

    def _connect(self, ws_url, token):
        """兼容 websockets 新旧版的 extra_headers / additional_headers。"""
        import websockets
        headers = {"Authorization": "Bearer " + token,
                   "Protocol-Version": "1",
                   "Device-Id": self.device_id,
                   "Client-Id": self.client_id}
        try:
            return websockets.connect(ws_url, additional_headers=headers,
                                      open_timeout=15, max_size=4 * 1024 * 1024)
        except TypeError:
            return websockets.connect(ws_url, extra_headers=headers,
                                      open_timeout=15, max_size=4 * 1024 * 1024)

    async def _reply_async(self, pcm):
        import asyncio
        import websockets
        import opuslib

        ws_url, token = self._resolve()
        if not ws_url:
            raise RuntimeError("小智 ws_url 为空，请在 chat_config.json 填 xiaozhi.ws_url")

        enc = opuslib.Encoder(SAMPLE_RATE, 1, opuslib.APPLICATION_AUDIO)
        down_rate = int(self.c.get("downlink_rate", 24000))
        dec = None
        heard, chunks, session_id = "", [], ""

        async with self._connect(ws_url, token) as ws:
            await ws.send(json.dumps({
                "type": "hello", "version": 1, "transport": "websocket",
                "features": {"mcp": False, "aec": False},
                "audio_params": {"format": "opus", "sample_rate": SAMPLE_RATE,
                                 "channels": 1, "frame_duration": 60}
            }, ensure_ascii=False))

            # 等服务端 hello；中间可能先来别的状态消息
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(msg, bytes):
                    continue
                d = json.loads(msg)
                if d.get("type") == "hello":
                    session_id = d.get("session_id", "")
                    ap = d.get("audio_params") or {}
                    down_rate = int(ap.get("sample_rate", down_rate))
                    break
            else:
                raise RuntimeError("小智服务端 10s 内没回 hello")

            await ws.send(json.dumps({"session_id": session_id, "type": "listen",
                                      "state": "start", "mode": "manual"},
                                     ensure_ascii=False))

            # 按 60ms 实时节奏推上行 Opus 帧，服务端 VAD 才判得准
            step = self.frame_samples * 2
            for off in range(0, len(pcm) - step + 1, step):
                frame = enc.encode(pcm[off:off + step], self.frame_samples)
                await ws.send(frame)
                await asyncio.sleep(0.06)

            await ws.send(json.dumps({"session_id": session_id, "type": "listen",
                                      "state": "stop"}, ensure_ascii=False))

            dec = opuslib.Decoder(down_rate, 1)
            down_step = down_rate * 60 // 1000
            speaking = False
            deadline = time.monotonic() + float(self.c.get("timeout", 40))
            while time.monotonic() < deadline:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(msg, bytes):
                    if not msg:
                        continue
                    try:
                        chunks.append(dec.decode(msg, down_step))
                    except Exception:
                        pass
                    continue
                d = json.loads(msg)
                t = d.get("type")
                if t == "stt":
                    heard = d.get("text", "")
                elif t == "tts":
                    state = d.get("state")
                    if state == "start":
                        speaking = True
                    elif state == "stop":
                        break
                elif t == "hello":
                    continue
            if not speaking and not chunks:
                raise RuntimeError("小智服务端没回 TTS 音频（检查服务端 ASR/LLM/TTS 配置）")

        down_pcm = resample_pcm(b"".join(chunks), down_rate, SAMPLE_RATE)
        return {"user_text": heard, "reply_text": "", "pcm": down_pcm,
                "skip": False, "tts": "xiaozhi-server"}

    def reply(self, pcm, user_text=None):
        import asyncio
        if user_text:
            raise RuntimeError("小智后端只收语音，文字调试请切到 backend=openai")
        return asyncio.run(self._reply_async(pcm))

    def say(self, text):
        raise RuntimeError("小智后端只收语音，文字调试请切到 backend=openai")


class MockBrain:
    """自检用：不联网、不接板子也能把状态机和音频管线跑一遍。"""
    name = "mock"

    def __init__(self, cfg=None):
        pass

    def check(self):
        return []

    def reply(self, pcm, user_text=None):
        text = user_text or "你好，我想聊聊天"
        return {"user_text": text, "reply_text": "我在呢，您慢慢说。",
                "pcm": tone_pcm(0.6), "skip": False, "tts": "mock"}

    def say(self, text):
        return self.reply(b"", user_text=text)


def make_brain(cfg, mock=False):
    if mock:
        return MockBrain(cfg)
    backend = str(cfg.get("backend", "openai")).lower()
    if backend == "xiaozhi":
        return XiaozhiBrain(cfg)
    return OpenAIBrain(cfg)


# ---------------------------------------------------------------------------
# 对话桥主体
# ---------------------------------------------------------------------------
class ChatBridge:
    """
    状态：idle -> listening -> thinking -> speaking -> listening
    thinking / speaking 期间丢弃上行，避免把 AI 自己的声音当成新一句（半双工）。
    固件侧 audio_board_set_downlink_active() 也会在下行播放时静音上行，双保险。
    """
    def __init__(self, device="", cfg=None, mock=False, port=None):
        self.cfg = apply_env(cfg or load_config())
        self.device = device or self.cfg.get("device") or "192.168.2.138"
        self.port = int(port or self.cfg.get("control_port", 8081))
        self.brain = make_brain(self.cfg, mock=mock)
        self.vad = VAD(self.cfg.get("vad"))
        self.max_down = float(self.cfg.get("down", {}).get("max_seconds", 20))
        self.down_timeout = float(self.cfg.get("down", {}).get("timeout", 90))

        self._stop = threading.Event()
        self._listen_thread = None
        self._worker_thread = None
        self._jobs = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._state = "idle"
        self._detail = ""
        self._rounds = []           # 最近 10 轮 {user, reply, tts, ts}
        self._error = ""
        self._started_at = 0.0
        self._stats = {"utterances": 0, "replies": 0, "errors": 0}

    # -- 状态 ---------------------------------------------------------------
    def _set(self, state, detail=""):
        with self._lock:
            self._state = state
            self._detail = detail

    def status(self):
        with self._lock:
            return {"running": not self._stop.is_set() and self._listen_thread is not None,
                    "state": self._state, "detail": self._detail,
                    "device": self.device, "port": self.port,
                    "backend": self.brain.name,
                    "config_problems": self.brain.check(),
                    "error": self._error,
                    "uptime": int(time.time() - self._started_at) if self._started_at else 0,
                    "rounds": list(self._rounds[-10:]),
                    "stats": dict(self._stats)}

    def _record(self, info):
        with self._lock:
            self._rounds.append({"user": info.get("user_text", ""),
                                 "reply": info.get("reply_text", ""),
                                 "tts": info.get("tts", ""),
                                 "ts": time.strftime("%H:%M:%S")})
            self._rounds = self._rounds[-10:]
            self._stats["replies"] += 1
            self._stats["utterances"] += 1

    # -- 生命周期 -----------------------------------------------------------
    def start(self):
        if self._listen_thread and self._listen_thread.is_alive():
            return {"ok": True, "msg": "已经在跑"}
        problems = self.brain.check()
        if problems:
            return {"ok": False, "msg": "配置未就绪", "problems": problems}
        self._error = ""
        self._stop.clear()
        self.vad.reset()
        self._started_at = time.time()
        self._listen_thread = threading.Thread(target=self._listen_loop,
                                               name="rcb-listen", daemon=True)
        self._worker_thread = threading.Thread(target=self._work_loop,
                                               name="rcb-work", daemon=True)
        self._listen_thread.start()
        self._worker_thread.start()
        self._set("listening", "等待老人说话…")
        return {"ok": True, "msg": "对话已开启"}

    def stop(self):
        self._stop.set()
        try:
            self._jobs.put_nowait(("__stop__", None))
        except queue.Full:
            pass
        self._set("idle", "已停止")
        return {"ok": True, "msg": "对话已停止"}

    def say(self, text):
        """文字直接问，绕开麦克风/ASR，用来验证 LLM→TTS→板子这一段。

        这里只起 worker，不拉 /audio/alaw —— 板子不在线时也能单独验证
        「大模型能不能回话、TTS 能不能出声」这两件事。
        """
        if not text.strip():
            return {"ok": False, "msg": "文字为空"}
        if not self._worker_thread or not self._worker_thread.is_alive():
            problems = self.brain.check()
            if problems:
                return {"ok": False, "msg": "配置未就绪", "problems": problems}
            self._stop.clear()
            self._started_at = self._started_at or time.time()
            self._worker_thread = threading.Thread(target=self._work_loop,
                                                   name="rcb-work", daemon=True)
            self._worker_thread.start()
        try:
            self._jobs.put_nowait(("text", text.strip()))
        except queue.Full:
            return {"ok": False, "msg": "上一句还没处理完"}
        return {"ok": True, "msg": "已提交"}

    # -- 上行：拉 /audio/alaw 长连接 ---------------------------------------
    def _listen_loop(self):
        url = "http://%s:%d/audio/alaw" % (self.device, self.port)
        backoff = 1
        while not self._stop.is_set():
            resp = None
            try:
                self._set("listening", "连接 %s" % url)
                resp = urllib.request.urlopen(url, timeout=5)
                backoff = 1
                while not self._stop.is_set():
                    try:
                        chunk = resp.read1(3200) if hasattr(resp, "read1") else resp.read(3200)
                    except (TimeoutError, OSError) as e:
                        # 读超时只是这一小段没数据；socket.timeout 在 3.10+ 就是
                        # TimeoutError。其它 OSError 才是真断线，交给外层重连。
                        if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
                            continue
                        raise
                    if not chunk:
                        raise ConnectionError("上行流结束")
                    pcm = alaw_to_pcm(chunk)
                    utterance = self.vad.feed(pcm)
                    if utterance and self._state in ("listening", "idle"):
                        try:
                            self._jobs.put_nowait(("audio", utterance))
                            self._set("thinking", "听到一句，正在识别…")
                        except queue.Full:
                            pass                     # 上一句没处理完，丢掉这句
            except Exception as e:
                if self._stop.is_set():
                    break
                self._error = "上行断开: %s" % e
                self._stats["errors"] += 1
                self._set("listening", "上行重连中（%s）" % str(e)[:60])
                time.sleep(min(backoff, 10))
                backoff = min(backoff * 2, 10)
            finally:
                if resp:
                    try:
                        resp.close()
                    except Exception:
                        pass
                self.vad.reset()

    # -- 下行：ASR/LLM/TTS -> POST /audio/down ------------------------------
    def _work_loop(self):
        while not self._stop.is_set():
            try:
                kind, payload = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            if kind == "__stop__":
                break
            try:
                self._handle(kind, payload)
            except Exception as e:
                self._error = str(e)
                self._stats["errors"] += 1
                print("[chat] 本轮失败: %s" % e, file=sys.stderr)
            finally:
                # stop() 之后别把状态又改回 listening
                if not self._stop.is_set() and self._state != "idle":
                    self._set("listening", "等待下一句…")

    def _handle(self, kind, payload):
        self._set("thinking", "识别 / 生成回复…")
        info = self.brain.say(payload) if kind == "text" else self.brain.reply(payload)
        if info.get("skip"):
            self._set("listening", "没听清，跳过")
            return

        pcm = info.get("pcm") or b""
        limit = int(self.max_down * SAMPLE_RATE) * 2
        if len(pcm) > limit:
            pcm = pcm[:limit]
        self._record(info)

        if not pcm:
            self._set("listening", "TTS 没产出音频")
            return
        self._set("speaking", "播放回复（%.1fs）" % (len(pcm) / (SAMPLE_RATE * 2)))
        # 固件的 /audio/down 按真实播放速率返回，timeout 必须覆盖整段音频
        timeout = max(self.down_timeout, len(pcm) / (SAMPLE_RATE * 2) + 20)
        post_down(self.device, self.port, pcm, timeout)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def selftest():
    """不接板子、不联网，验证 A-law 解码 / VAD / 状态机 / PCM 生成。"""
    print("== chat_bridge 自检 ==")
    # 1. A-law 与 audioop（若有）对照
    try:
        import audioop
        raw = bytes(range(256))
        ref = audioop.alaw2lin(raw, 2)
        got = alaw_to_pcm(raw)
        diff = sum(1 for i in range(256)
                   if struct.unpack_from("<h", ref, i * 2)[0] !=
                      struct.unpack_from("<h", got, i * 2)[0])
        print("1. A-law 解码对照 audioop: %s (%d/256 不一致)" %
              ("通过" if diff == 0 else "有偏差", diff))
    except ImportError:
        print("1. A-law 解码: 内置表已生成（无 audioop，跳过对照）")

    # 2. VAD：用 1s 正弦 + 1s 静音喂进去，应切出一句
    vad = VAD()
    speech = tone_pcm(1.0, 440, 12000)
    silence = bytes(SAMPLE_RATE * 2)
    got = None
    for off in range(0, len(speech + silence), 640):
        u = vad.feed((speech + silence)[off:off + 640])
        if u:
            got = u
            break
    ok = got is not None and len(got) >= 0.4 * SAMPLE_RATE * 2
    print("2. VAD 断句: %s (%s)" % ("通过" if ok else "失败",
                                    "%.2fs" % (len(got) / 32000) if got else "未切出"))

    # 3. Mock 全链路
    brain = MockBrain()
    info = brain.reply(got or speech)
    print("3. Mock 对话: 用户=%r 回复=%r 下行=%d 字节" %
          (info["user_text"], info["reply_text"], len(info["pcm"])))

    # 4. 重采样
    p24 = tone_pcm(1.0, 440, 8000)
    pcm16 = resample_pcm(p24, 24000, 16000)
    print("4. 24k->16k 重采样: %s (%d -> %d 字节)" %
          ("通过" if len(pcm16) < len(p24) else "失败", len(p24), len(pcm16)))

    # 5. 配置与小智协议依赖
    cfg = load_config()
    xz = XiaozhiBrain(cfg)
    print("5. 小智后端依赖检查: " + ("; ".join(xz.check()) or "全部就绪"))
    print("== 自检结束 ==")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="远程看护盒子 · 语言大模型对话桥")
    ap.add_argument("--device", default="", help="板子 IP，默认用 chat_config.json")
    ap.add_argument("--port", type=int, default=0, help="控制面端口，默认 8081")
    ap.add_argument("--config", default="", help="指定配置文件")
    ap.add_argument("--say", default="", help="文字问一句（绕开 ASR，调试 LLM/TTS/下行）")
    ap.add_argument("--selftest", action="store_true", help="不接板子，跑本地自检")
    ap.add_argument("--write-example", action="store_true", help="生成配置模板")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.write_example:
        print("已生成:", write_example())
        return 0

    cfg = load_config(args.config or None)
    bridge = ChatBridge(device=args.device, cfg=cfg,
                        port=args.port or None, mock=False)
    if args.say:
        r = bridge.say(args.say)
        print(json.dumps(r, ensure_ascii=False))
        deadline = time.time() + 60
        while time.time() < deadline:
            s = bridge.status()
            if s["rounds"] or s["error"]:
                break
            time.sleep(0.5)
        print(json.dumps(bridge.status(), ensure_ascii=False, indent=2))
        bridge.stop()
        return 0

    r = bridge.start()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if not r["ok"]:
        return 1
    print("对话已开启，Ctrl+C 退出。状态字段：", bridge.status()["state"])
    try:
        while True:
            time.sleep(3)
            s = bridge.status()
            print("[%s] %s | 轮次=%d 错误=%d %s" %
                  (time.strftime("%H:%M:%S"), s["state"], s["stats"]["replies"],
                   s["stats"]["errors"], s["error"]))
    except KeyboardInterrupt:
        bridge.stop()
        print("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
