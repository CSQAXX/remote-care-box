# 远程看护盒子 · 语言大模型对话桥

> 让盒子能听、能想、能说，**一行固件代码都不用改，也不用重烧**。
> 控制台页面第 ④ 块「AI 对话」点一下就能开。

---

## 0. 先说清楚「32 自带的小智」是什么

正点原子 ATK-DNESP32S3-BOX3 出厂刷的是 `78/xiaozhi-esp32` **一整套独立固件**，
配套一个跑 ASR / LLM / TTS 的服务端（`xiaozhi-esp32-server` 或官方 xiaozhi.me）。

**刷回官方固件 = 现在这套自研固件全没了**：`main.c` 里的摄像头、活动检测、
电源管理、MJPEG、`/audio/down` 全部被覆盖，看护产品直接归零。这不是"接个小智库"，
是换了个工程。

所以这里走的是第三条路：

| | 谁做 ASR/LLM/TTS | 固件 | 看护功能 |
| --- | --- | --- | --- |
| 刷官方小智固件 | 小智服务端 | 换成官方固件 | ❌ 全丢 |
| **本项目对话桥** | 云端大模型 或 小智服务端 | **原样保留** | ✅ 全保留 |
| 移植小智协议栈进固件 | 小智服务端 | 大改 | ✅ 但工作量大 |

做法：PC 端拉板子现成的 `/audio/alaw`，在本地断句，把音频交给大脑，
再把回复灌回现成的 `/audio/down`。**大脑可以随便换**，其中就包括
「把 PC 模拟成一台小智设备去连小智服务端」——能力一样，固件不动。

```
板子麦克风 ──GET /audio/alaw (G.711 A-law 16k, 长连接)──┐
                                                        ▼
PC: A-law 解码 ── VAD 断句 ── ASR ── LLM(多轮) ── TTS ── PCM16 16k
                                                        │
板子喇叭   ◄──POST /audio/down (立即播放)────────────────┘
```

固件在 `/audio/down` 播放期间会调 `audio_board_set_downlink_active()` 静音上行，
对话桥自己也在 thinking / speaking 阶段丢弃上行——**双保险，不会自己听到自己**。

---

## 1. 快速起步（默认 `backend: openai`）

### 1.1 建配置

```powershell
cd D:\Software\kanhu\remote-care-box\pc_receiver
copy chat_config.example.json chat_config.json
```

`chat_config.json` 已被 `.gitignore` 排除，**key 不会进 git**。
也可以不写文件，用环境变量（优先级更高）：

```powershell
$env:LLM_API_KEY="sk-xxx"; $env:ASR_API_KEY="sk-yyy"
```

支持的环境变量：`CHAT_BACKEND`、`LLM_API_KEY/LLM_BASE_URL/LLM_MODEL`、
`ASR_API_KEY/ASR_BASE_URL/ASR_MODEL`、`TTS_API_KEY/TTS_BASE_URL/TTS_MODEL`、
`XIAOZHI_WS_URL`、`XIAOZHI_TOKEN`。

### 1.2 最少要填两项

| 字段 | 说明 |
| --- | --- |
| `asr.api_key` + `asr.base_url` + `asr.model` | 语音转文字。任何兼容 OpenAI `/audio/transcriptions` 的服务都行（示例配的是硅基流动 SenseVoiceSmall） |
| `llm.api_key` + `llm.base_url` + `llm.model` | 大模型。DeepSeek / 通义 / 豆包 / 本地 Ollama 只要兼容 `/chat/completions` 都能接 |

`tts` 可以**什么都不填**：`provider: auto` 会依次试
`openai → edge-tts → Windows SAPI → 提示音`，中文 Windows 一般自带中文语音包，
零 key 也能出声。想换更好的音色再配 TTS。

### 1.3 跑起来

```powershell
python pc_console.py --device 192.168.2.138 --port 8899
# 浏览器打开 http://127.0.0.1:8899
# 第 ④ 块卡片 → 「开启 AI 对话」→ 对着板子说话
```

**先用「文字问一句」调试**：它绕开麦克风和 ASR，直接验证
`LLM → TTS → /audio/down → 板子出声` 这一段，能最快分清是识别的问题还是播放的问题。

### 1.4 不用界面也能跑

```powershell
python chat_bridge.py --device 192.168.2.138 --say "今天感觉怎么样"
python chat_bridge.py --device 192.168.2.138          # 一直听，Ctrl+C 退出
```

---

## 2. 接小智（`backend: xiaozhi`）

这一步把 PC 端模拟成一台小智设备，连你自己部署的 `xiaozhi-esp32-server`。
**服务端负责 VAD / ASR / LLM / TTS / MCP，PC 端只做音频搬运。**

### 2.1 装可选依赖

```powershell
pip install websockets opuslib
```

> ⚠️ Windows 上 `opuslib` 只是绑定，**还需要 `libopus` DLL**（`opus.dll`）。
> 放到 PATH 里的某个目录或 `pc_receiver/` 下。这是小智后端最大的落地门槛。
> 缺依赖时页面「配置待补」会直接列出来，不会静默失败。

### 2.2 配 `chat_config.json`

```json
{
  "backend": "xiaozhi",
  "xiaozhi": {
    "ws_url": "ws://192.168.2.20:8000/xiaozhi/v1/",
    "token": "your-token",
    "device_id": "板子MAC",
    "client_id": "任意UUID"
  }
}
```

也可以只填 `ota_url`（如 `http://<server>:8002/xiaozhi/ota/`），
对话桥会带 `Device-Id` / `Client-Id` 去问服务端要 `websocket.url` 和 `token`。

### 2.3 协议实现到哪一步

已按 `78/xiaozhi-esp32` 官方 `docs/websocket.md` + xiaozhi.me 通信文档实现：

- 握手 headers：`Authorization: Bearer <token>`、`Protocol-Version: 1`、
  `Device-Id`、`Client-Id`
- `hello`（上行 Opus 16k / 单声道 / 60ms 帧），等服务端 `hello` 拿到下行采样率
- `listen start(mode=manual) → 按 60ms 实时节奏推二进制 Opus 帧 → listen stop`
- 收 `stt` / `tts(start|stop)` JSON，二进制帧解码后
  **从 24k 重采样到 16k** 再 POST 给板子

> 目前是「一句一连接」：每轮重新握手，简单可靠、断线好恢复，
> 代价是多 1~2 秒握手。要做成常驻长连接（唤醒词、连续对话、MCP 控设备）
> 是下一步的事，接口已经按后端抽象好。

> 官方 `xiaozhi.me` 公共服务需要设备激活码并校验 `Device-Id`，
> 用自建服务端最省事。别拿模拟设备去蹭公共服务，容易被封。

---

## 3. 配置字段速查

| 段 | 字段 | 默认 | 说明 |
| --- | --- | --- | --- |
| 顶层 | `backend` | `openai` | `openai` / `xiaozhi` |
| | `device` | 空 | 空则用 `pc_console.py --device` |
| | `control_port` | `8081` | 控制面，`/audio/alaw` 与 `/audio/down` 都在这 |
| `vad` | `energy_threshold` | `350` | 20ms 帧 rms 门限。板上实测静音 ~21、人声 ~1000，太灵敏就调大 |
| | `silence_ms` | `700` | 连续静音多久算说完 |
| | `min_speech_ms` | `400` | 短于这个丢掉（咳嗽、碰撞） |
| | `max_speech_ms` | `12000` | 超时强制断句 |
| | `preroll_ms` | `300` | 起句前留一段，避免吃掉第一个字 |
| `llm` | `system_prompt` | 养老陪伴人设 | 强制口语化、≤60 字，方便 TTS 播 |
| | `history_turns` | `6` | 记住最近几轮 |
| `tts` | `provider` | `auto` | `auto` / `openai` / `edge` / `sapi` / `mock` |
| `down` | `max_seconds` | `20` | 单次回复最长播放秒数 |
| | `timeout` | `90` | 固件按真实播放速率返回，超时要留足 |

---

## 4. 自检与排障

```powershell
python chat_bridge.py --selftest       # 不接板子不联网，验解码 / VAD / 重采样 / 依赖
python test_chat_bridge.py            # 11 项离线验收（含控制台页面与四个接口）
```

| 现象 | 先查哪 |
| --- | --- |
| 点「开启 AI 对话」被拒绝 | 卡片里「配置待补」会列出缺哪个 key |
| 开启后一直「上行重连中」 | 板子 IP / 8081 控制面不通；`curl http://<ip>:8081/health` |
| 听到了但没反应 | `vad.energy_threshold` 调小；或先用「文字问一句」隔离 ASR |
| 「正在想」卡住不说话 | LLM key / 网络 / `base_url`；看控制台 stderr |
| 回复是「滴滴」提示音 | TTS 全降级了，看 stderr 的 `[chat][tts] 已降级到 ...`，装中文语音包或配 TTS |
| 小智后端连不上 | `ws_url` / token / `Device-Id`；`opus.dll` 是否就位 |
| Python 3.13 报 `audioop` 不存在 | 对话桥自带解码表，不依赖它；控制台原有的「录一段老人语音」也已改用内置表 |

---

## 5. 已知限制

- **半双工**：按住说话 / AI 说话期间不同时听，靠状态机 + 固件静音规避啸叫。
  全双工 AEC 仍是 v2 的事。
- **VAD 是能量门限**，不是 Silero VAD：安静环境够用，电视声、风扇声可能误触发，
  后续可换成 `webrtcvad` / Silero。
- **一句一问**：没有唤醒词、没有打断（barge-in）。小智后端常驻连接后可补。
- **所有音频走局域网 HTTP**，没有鉴权、没有 TLS。上公网前必须补（P2 阶段）。
- `xiaozhi` 后端的 `llm` / `asr` / `tts` 配置段不生效，全部由服务端决定。

---

## 6. 文件清单

| 文件 | 作用 |
| --- | --- |
| `chat_bridge.py` | 对话桥核心：解码 / VAD / ASR / LLM / TTS / 小智 WS，纯标准库 |
| `chat_config.example.json` | 配置模板（复制成 `chat_config.json` 后填 key） |
| `test_chat_bridge.py` | 离线验收 11 项 |
| `pc_console.py` | 控制台：新增第 ④ 块卡片 + `/api/chat/*` 四个接口 |
| `README_chat.md` | 本文 |

**固件一行没动**：`firmware/` 零改动，现有摄像头、活动检测、电源管理、
MJPEG、对讲、求助按键全部照常工作。
