# 交接文档：「小电脑」语音遥控模块（给 Cursor）

> 生成时间：2026-11-08 22:20
> 仓库：`D:\Software\kanhu\remote-care-box`（当前分支 `main`）
> 设备：正点原子 ATK-DNESP32S3-BOX3，ESP32-S3，16MB Flash，串口 `COM3`，局域网 IP `192.168.2.138`
> 电脑端：`D:\Software\local-dev\app`（Python，8767 端口）
> 一句话：**喊「小电脑」，盒子把这句话送到我自己的电脑执行（如打开微信），再把电脑的回话念出来 + 显示在屏幕上。**

---

## 0. 三条红线（先读这段，违反会出事）

### 红线 1：老人看护逻辑一行都不能改

这套固件原本是**养老看护盒子**，现在多装了一个「小电脑」叫法。老人那条链路必须保持原样：

| 不能动 | 原因 |
| --- | --- |
| `firmware/main/ai_client.c` / `.h` | 老人对讲的整条链路（/v1/turn、状态机、连接）。小电脑只**读** `ai_client_get_state()` 判断要不要让路 |
| `firmware/main/voice_mode.c` / `.h` | 「晴老师」/「小康同学」的模式分发 |
| `firmware/main/audio_board.c` / `.h` | 麦克风/喇叭驱动。只**调用**现有接口，不改实现 |
| `lcd_prompt_show()` 的默认配色 | 老人提示是**黑底黄字**（求助红字）。小电脑的白底黑字走新接口 `lcd_prompt_show_styled()` |
| `firmware/main/main.c` | 加小电脑模块时一个字都没动。**不要为了"整理"去动它** |

判断标准：改完之后，`POST http://192.168.2.138:8081/cmd` 三条老人提示必须仍然是 `fg=0xFFE0 bg=0x0000`（黄字黑底）。验证脚本见 §6。

### 红线 2：不许碰整片备份

```
D:\Software\kanhu\xiaozhi-box3\backup\original_full_16MB.bin
```

这是刷任何东西**之前**的原厂整片（16,777,216 字节），唯一的后悔药。只读，不覆写、不删除、不移动。

### 红线 3：新代码必须能用宏一键关掉

所有新增代码都包在 `ENABLE_PC_VOICE` 里。关掉之后，编译出来的固件必须和加这个功能之前**完全一致**。

```bat
REM 关掉小电脑功能（保留代码）
firmware\idf_build.bat -DENABLE_PC_VOICE=0 build
```

开关位置：`firmware/main/CMakeLists.txt:45-56`。

---

## 1. 一条完整链路（先看懂这个再改代码）

```
  老人/我 说「小电脑，打开微信」
        │
        ▼
  ┌─────────────────────────────────────────────┐
  │ 板子 firmware/main/wake_word.c:211-245      │
  │ esp-sr MultiNet7 命中 command_id = 316      │
  │ （314=晴老师 315=小康同学 316=小电脑）       │
  └──────────────┬──────────────────────────────┘
                 │ pc_voice_start_turn_at(起句点)
                 ▼
  ┌─────────────────────────────────────────────┐
  │ firmware/main/pc_voice.c                    │
  │ ① 独立读游标录音，VAD 断句（静音 500ms 收尾）│
  │ ② 16kHz → 8kHz（两两解码取平均）            │
  │ ③ 原始 socket 连 电脑:8767                  │
  │    POST /voice  body = G.711 A-law          │
  │    X-IoT-Token / X-Want-Audio: 1            │
  └──────────────┬──────────────────────────────┘
                 │
                 ▼
  ┌─────────────────────────────────────────────┐
  │ 电脑 D:\Software\local-dev\app\iot_bridge.py│
  │ ④ 收齐正文 → 立刻回 "HTTP/1.1 100 Continue" │
  │ ⑤ 本地 ASR（voice.py）                      │
  │ ⑥ quick_route.py 白名单 → 真开软件/网址     │
  │    没命中 → 转 Agent / 本地模型             │
  │ ⑦ TTS 合成 PCM16 → 写进响应 body           │
  │    识别文本/回话放 X-User-Text/X-Reply-Text │
  └──────────────┬──────────────────────────────┘
                 │
                 ▼
  ┌─────────────────────────────────────────────┐
  │ 板子：边收边播 + 屏幕白底黑字两行字幕        │
  └─────────────────────────────────────────────┘
```

**关键时间预算**（实测，别按"应该很快"去设计）：

| 阶段 | 实测耗时（真实串口日志） |
| --- | --- |
| TCP connect | 7 ~ 19 ms |
| 上传 14KB A-law | 19 ~ 20 ms（改平滑发送前是 **52 秒**） |
| 电脑回 `100 Continue` | 距发完约 1 秒 |
| 电脑 ASR + 执行 + TTS | 3 ~ 4 秒（quick 路由）；走 Agent 可达 20 秒 |
| 回传 PCM16 | 186 ~ 338 KB，边收边播，播完约 4 秒 |
| 整轮（喊话 → 播完） | **11 ~ 13 秒**（quick 路由）/ 20~25 秒（走 Agent） |

所以板子等响应头是 **45 秒超时**，屏幕必须在这期间一直有字（「电脑在想…」→「电脑收到了，正在办…」），不然用户以为死机。

---

## 2. 目录地图：改哪里 / 别碰哪里

### 2.1 板子端（`D:\Software\kanhu\remote-care-box\firmware`）

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `main/pc_voice.c` | **新增 1001 行** | 核心：录音、降采样、socket 上传、收流播放、字幕 |
| `main/pc_voice.h` | 新增 | 对外接口 + 大段设计说明（**先读这个的注释**） |
| `main/pc_voice_config.h` | 新增、**不提交** | 电脑 IP + 8767 口令，由 `tools/enable_pc_voice.py` 生成 |
| `main/wake_word.c` | 改 3 处 | 211 行 `#ifdef` 包住 316 判断；224-245 行「小电脑」单独一条路；305 行开机建工作线程 |
| `main/wake_word.h` | 改 1 处 | 44-48 行 3 个宏：名字/拼音/`WAKE_WORD_PC_CMD_ID 316` |
| `main/lcd_prompt.h` | 改 2 处 | 加 `PROMPT_PC` 枚举；加 `lcd_prompt_show_styled()` 声明 |
| `main/lcd_prompt.c` | 改 2 处 | `draw_banner()` 支持任意 fg/bg + 两行；`lcd_prompt_show()` 改为内部转调 styled |
| `main/lcd_font.c` | 改 | 字形 149 → **580**（汉字 476）。原 149 个逐字节未变 |
| `main/CMakeLists.txt` | 改 | 45-56 行开关段 |
| `sdkconfig` / `sdkconfig.defaults` | 改 | `CONFIG_LWIP_TCP_SND_BUF_DEFAULT=16384`、`CONFIG_LWIP_TCP_WND_DEFAULT=16384` |

### 2.2 电脑端（`D:\Software\local-dev\app`）

| 文件 | 作用 |
| --- | --- |
| `iot_bridge.py` | 8767 服务。`_post_voice()` 在 698 行是 `/voice` 入口；716 行回 100 Continue；593-594 行写 `X-User-Text`/`X-Reply-Text` |
| `quick_route.py` | 「打开 X」的确定性白名单路由（`_SITES` 别名表 + `_OPEN` 正则），命中就直接执行，<1ms |
| `local_apps.py` | 扫本机装了哪些客户端（Weixin.exe 等），决定开客户端还是开网页 |
| `voice.py` | 本地 ASR / TTS |
| `server.py` | 主服务，`/api/iot/restart`、`/api/iot/status` |

### 2.3 工具脚本（`firmware/tools/`）

| 脚本 | 用途 |
| --- | --- |
| `enable_pc_voice.py` | 打补丁（备份 + 生成 config + 插入代码）。`--dry-run` 只看不改，`--revert` 还原 |
| `gen_font_extra.py` | 补字库，**只追加**，可重复跑。会自动从 `local-dev/logs/iot.log` 收集电脑实际回过的字 |
| `check_srmodels.py` | 校验打包后的 `srmodels.bin` 里命令词条数。**每次 build_models 后必跑**（词表太短会 Guru Meditation 无限重启） |
| `pc_voice_test.py` | 硬复位 + SAPI 念口令 + 抓串口日志到 `output/pc_voice_test.txt` |
| `lcd_prompt_check.py` | 老人提示配色回归（三条 /cmd 全过才算安全） |
| `raw_probe.py` / `probe_8767_raw.py` | 网络层探针，判断卡在板子还是电脑 |

---

## 3. 已完成 & 已实测（这些别重做）

### 3.1 已跑通

- 「小电脑，打开微信」→ 微信进程真的启动（进程号 50796，21:02:55）→ 板子念回话 → 屏幕白底黑字两行
- 「小电脑，打开百度」→ 打开默认浏览器百度首页，字幕「已为您在默认浏览器打开百度首页。需」/「要其他帮助吗？」共 24 字完整无乱码
- 老人三条提示（watching / content / calling）配色与文案零改动

### 3.2 字幕现状

```c
#define PC_SUBTITLE_FG   0x0000   /* 黑字 */
#define PC_SUBTITLE_BG   0xFFFF   /* 白底 */
```

- 横幅高 40px，两行各 20px，每行 16px 字，每行最多 **17 个汉字**，共 34 字
- 白底时顶端加 1px 灰线 `0xC618` 做分隔（黑底不加，老人那边保持原样）
- 超长自动在第二行末尾补「…」
- 自动跳过 `\n \r \t * # \``（LLM 爱输出 markdown，屏上没字形还让字幕变脏）

### 3.3 未做（别以为已经有了）

- 「小康同学」→ 小智模式目前**仍走 `ai_client` 的 /v1/turn**，没有接小智云端 WebSocket 协议。见 `voice_mode.c` 里的 `TODO(第二阶段)`
- 小电脑只支持一句话指令，不支持多轮

---

## 4. 关键设计决策（每条都是踩坑换来的，改之前先看"为什么"）

> 这一节是本文档最值钱的部分。想"优化"下面任何一条之前，先确认你已经理解它解决的问题。

### 4.1 传输层：不用 `esp_http_client`，用原始 socket

**现象**：用 `esp_http_client_write()` 上传时，串口没有任何输出，卡住不动。
**原因**：它内部靠 `select()` 等 socket 可写；在这条 Wi-Fi 链路上每次发 4~8KB 就 select 超时一次，超时时长正好等于设的 timeout，于是永远停在"发了一半"。
**现状**：`pc_http_turn()`（`pc_voice.c:521`）改用 `socket/connect/send/recv` + `SO_SNDTIMEO`/`SO_RCVTIMEO`。

### 4.2 TCP 发送缓冲必须 ≥ 一次要发的量

**现象**：每次精确卡在 **16384 字节**不动。
**原因**：lwIP 默认 `TCP_SND_BUF` 是 5760，窗口一满 `send()` 就阻塞等 ACK，而这条链路 ACK 几十秒不回。
**现状**：`CONFIG_LWIP_TCP_SND_BUF_DEFAULT` / `TCP_WND_DEFAULT` 都设 16384；音频硬上限 `PC_UPLINK_MAX 14000` 留余量。**超过上限就截尾，宁可少半句话也不能整句发不出去。**

### 4.3 发送要"平滑"：≤1KB 一块 + 块间 8ms

**现象**：一次 `send()` 把 33KB 全塞给 lwIP，lwIP 一股脑丢给 Wi-Fi 驱动，2.4G 拥挤信道直接雪崩丢包，**52 秒只推进 22KB**。
**现状**：`pc_send_all(..., smooth=true)`（`pc_voice.c:446`）每块最多 1KB，`vTaskDelay(8ms)`。速率约 125KB/s，丢包少了总耗时反而从 52 秒降到 **20 毫秒**（14KB 实测）。

### 4.4 上传降到 8kHz，但必须"两两解码取平均"

**现象**：直接抽偶数点 → 混叠 → 「打开微信」被识别成 **"I wish you"**，识别彻底崩。
**原因**：16k→8k 直接抽取相当于没做抗混叠，4~8kHz 成分折回 0~4kHz。
**现状**：`pc_downsample_8k()`（`pc_voice.c:761`）两个 A-law 样本先解码成 PCM16、取平均、再编码。相当于一个简单低通。

### 4.5 两种失败必须分开，绝不能笼统"重试"

**现象**：微信被**打开两次**。
**原因**：板子没等到回话就当失败重发，但电脑其实已经执行了。
**现状**（`pc_voice.c:91-92`）：

```c
#define PC_ERR_RETRY    (-1)   /* 连接/发送阶段就断了，电脑肯定没收到 → 重发安全 */
#define PC_ERR_GAVE_UP  (-2)   /* 音频已整段送到，只是没等到回话 → 绝不重发 */
```

配合电脑端的 **`HTTP/1.1 100 Continue`**（`iot_bridge.py:716`）：电脑收齐正文立刻回这个，板子才知道"音频送到了"。
判定逻辑：`一个字节都没收到` → 可重发；`收到确认但没等到响应头` → 只提示「电脑收到了，没回话」，不重发。

### 4.6 超时：收 45 秒，发 10 秒

**现象**：微信明明开了，盒子却说"没回话"。
**原因**：电脑读完音频要 ~19 秒，加识别+执行+合成共 20.7 秒才吐响应，板子只等 10 秒就放弃。
**现状**：`SO_RCVTIMEO = 45s`，`SO_SNDTIMEO = 10s`，发送预算 60 秒。

### 4.7 传输期间关 Wi-Fi 省电

`esp_wifi_set_ps(WIFI_PS_NONE)` 在 `pc_http_turn()` 开头，结束时还回 `WIFI_PS_MIN_MODEM`。睡了 ACK 收不及时。

### 4.8 并发控制必须是**二值信号量**，不能是互斥量

**现象**：`assert failed: xTaskPriorityDisinherit (tasks.c:5147)` → 板子重启，失败路径上必现。
**原因**：这一轮是"唤醒任务 take、工作线程 give"，跨任务交接。互斥量只能持有者自己释放。
**现状**：`xSemaphoreCreateBinary()` + 创建时 give 一次表示空闲（`pc_voice.c:921`）。

### 4.9 工作线程必须**开机建好**，不能唤醒时现建

**现象**：喊「小电脑」完全没反应，日志 `pc_voice: 任务创建失败`。
**原因**：运行期 `xTaskCreate` 要一段连续内部 RAM；摄像头/TLS 跑过之后内部堆碎片化，8KB 栈建不出来。
**现状**：`pc_voice_prepare()` 在 `wake_word_start()` 里调一次（`wake_word.c:305`），之后每轮只发信号。

### 4.10 大缓冲一律放 PSRAM

内部 RAM 太紧（曾只剩 28KB）。以下全部 `heap_caps_malloc(..., MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)`：

- 录音缓冲 10 秒 × 16k = 160KB
- 响应头 4KB、PCM 4KB
- 提示音缓冲

放栈上 → `xTaskCreate` 失败；放静态区 → 挤爆开机任务。**两种表现都是"喊了没反应"，还会连带 ai_client 发送失败。**

### 4.11 屏幕字库缺字是**静默跳过**，这是"闪一下黑"的真凶

**现象**：喊「小电脑」，屏幕闪一下黑，什么都没显示。
**原因**：`lcd_font.c` 原本只有 149 个字形（汉字仅 57 个）。`glyph_find()` 返回 NULL 时渲染**直接留白**，横幅铺了黑底但一个字没画。
**现状**：580 字形（汉字 476）。补字用 `tools/gen_font_extra.py`，**只追加，不动已有条目**（原 149 个已逐字节校验）。

→ **以后任何新文案上屏前，先跑 `gen_font_extra.py` 再 build。**

### 4.12 字幕必须按 UTF-8 边界裁，不能按字节

**现象**：屏幕上出现乱码方块。
**原因**：`snprintf` 按字节截断会把一个汉字切一半。
**现状**：`pc_fit_two_lines()`（`pc_voice.c:278`）按 UTF-8 字符边界切。

### 4.13 响应头里的中文是 URL 编码，一个汉字 9 字节

**现象**：字幕只有半句 + 方块。
**原因**：`reply_buf[192]` 只装得下 21 个汉字（192 ÷ 9），长句被切在汉字 3 字节中间。
**现状**：`PC_TEXT_BUF 512` / `PC_REPLY_BUF 1536` / `PC_HEAD_BUF 4096`；`pc_url_decode()` 加"半字符保底"——剩余容量装不下整个字符就停，宁可少显示一个字。

### 4.14 `0x0000` 不能当成"没指定颜色"

**现象**：白底黄字，看不见。
**原因**：`lcd_prompt_show_styled()` 早期把 `fg == 0` 当"未指定"自动选色（老人那边黄字是这么算的）。
**现状**：`show_styled()` 按**原值**使用颜色；自动选色下沉到老人接口 `lcd_prompt_show()` 内部（`lcd_prompt.h` 注释里写明了）。

### 4.15 用独立 `PROMPT_PC` 类型隔离两套提示

类型不同 → `lcd_prompt_show_styled()` 换提示时会先擦旧横幅，不会和老人提示叠字；也保证改小电脑配色时碰不到老人那边。

---

## 5. 编译 / 打包 / 烧录（照抄，别自己发明）

### 5.1 前提

- ESP-IDF：`D:\Development Environment\ESP-IDF\esp-idf`（`idf_build.bat` 会 `export.bat`）
- Python（跑 esptool）：`D:\Development Environment\ESP-IDF\python_env\Scripts\python.exe`
- 烧录前**关掉所有串口工具**（串口助手、`idf.py monitor`、`diag_audio.py`），否则抢 `COM3` 直接失败

### 5.2 首次 / 词表改过之后：顺序不能乱

```bat
cd /d D:\Software\kanhu\remote-care-box\firmware

REM ① 重建词表（把 commands_cn.txt 编译成 srmodels.bin，约 3~5 分钟）
idf_build.bat build_models

REM ② 校验词表（条数必须是 316，少了会 Guru Meditation 无限重启）
python tools\check_srmodels.py

REM ③ 编译
idf_build.bat build
```

### 5.3 只改了 C 代码（最常见）

```bat
firmware\idf_build.bat build
firmware\idf_build.bat -p COM3 flash monitor
```

`idf.py flash` 会写：bootloader `0x0`、分区表 `0x8000`、ota_data `0xe000`、app `0x20000`、**srmodels `0x320000`**。
**不写 NVS `0x9000`**，所以 Wi-Fi 凭据和小电脑配置都保留。

### 5.4 改了配置（IP / token）

```bat
python tools\enable_pc_voice.py --host 192.168.2.135
python tools\enable_pc_voice.py --dry-run    REM 只看会改什么
python tools\enable_pc_voice.py --revert     REM 还原
```

改完必须重跑 `build`。也可以运行时覆盖（写 NVS，不用重烧）：`pc_voice_set_config(host, token)`，目前**没有**串口命令接它。

### 5.5 ⚠️ 命令词表的致命细节

改「小电脑」这个词、或加第 4 个叫法时，**必须改对文件**：

| 路径 | 行数 | 是不是真正打包的 |
| --- | --- | --- |
| `firmware/model/fst/commands_cn.txt` | 315（**缺 316**） | ❌ 不是。改它没用 |
| `managed_components/espressif__esp-sr/model/multinet_model/fst/commands_cn.txt` | **316（有 xiao dian nao）** | ✅ **是这份**（`movemodel.py` copytree 的是它） |

⚠️ `managed_components/` 是 `idf.py` 拉下来的依赖目录，**一旦被清空重拉，316 条就没了**，表现为「小电脑」突然喊不醒。
恢复：`python tools\enable_pc_voice.py` → `idf_build.bat build_models` → `check_srmodels.py` 确认 316 条。

拼音格式是**无声调**：`316,xiao dian nao`（不是 `xiao3 dian4 nao3`）。mn7 词表里没有声调 token。

---

## 6. 验证清单（改完一项勾一项）

### 6.1 看护侧没被带坏（最重要）

```powershell
# 三条都必须是 fg=0xFFE0 bg=0x0000（黄字黑底）
curl -s -X POST http://192.168.2.138:8081/cmd -d '{"cmd":"watching"}'
curl -s -X POST http://192.168.2.138:8081/cmd -d '{"cmd":"content","text":"该活动一下"}'
curl -s -X POST http://192.168.2.138:8081/cmd -d '{"cmd":"calling"}'
```

或直接跑 `python firmware\tools\lcd_prompt_check.py`。

> 注：`/cmd` 的 JSON 解析**不支持 `\uXXXX` 转义**，中文要直接写 UTF-8，否则原样上屏。

### 6.2 小电脑链路

```bat
REM 自动：硬复位 + 电脑喇叭念「小电脑，打开微信」+ 抓日志
python firmware\tools\pc_voice_test.py "小电脑，打开微信"
```

串口日志按顺序应出现：

```
pc_voice: 起句位置 ... (唤醒词接续)
pc_voice: 起句 (能量 ...)
pc_voice: 说完 (共 xxxx 字节 A-law)
pc_voice: 降到 8kHz: xxxx -> xxxx 字节
pc_voice: 连上 192.168.2.135:8767 (xx ms)
pc_voice: 请求发完, 等电脑回话 (上传耗时 xx ms)
pc_voice: 电脑已收到音频, 开始处理
pc_voice: 电脑回 HTTP 200 (音频 xxxxx 字节)
pc_voice: 回话播完 (xxxxx 字节 PCM16)
```

**跑对了长这样**（`output/pc_voice_test.txt` 里一次成功实录，可直接对照）：

```
39.79  wake_word: 命中「小电脑」#2 (概率 0.344) -> 交给我的电脑
39.81  pc_voice: 起句位置 442880 (唤醒词接续)
39.81  pc_voice: 起句 (能量 2138, 预roll 960 样本)
41.56  pc_voice: 说完 (共 28736 字节 A-law)
41.56  pc_voice: 降到 8kHz: 28736 -> 14000 字节      ← 触发了截尾保护，正常
41.58  pc_voice: 连上 192.168.2.135:8767 (7 ms)
41.61  pc_voice: 请求发完, 等电脑回话 (上传耗时 20 ms)
42.62  pc_voice: 电脑已收到音频, 开始处理
45.61  pc_voice: 电脑回 HTTP 200 (音频 189378 字节, 已到 3575)
49.90  pc_voice: 回话播完 (189378 字节 PCM16)
```

注意 `命中概率 0.344`：门限是 `WAKE_WORD_THRESHOLD 0.30f`，为了让老人发音也能唤醒而放得很宽，所以 0.3~0.4 属于正常，不是"识别不准"。

电脑端对应日志（`local-dev/logs/iot.log`）：

```
[iot] /voice 收到请求: ip=192.168.2.138 声明 xxxx 字节
[iot] /voice 收齐正文 xxxx/xxxx 字节 (等正文 xx ms)
```

### 6.3 屏幕字幕

串口里应有 `画横幅 fg=0x0000 bg=0xFFFF`，肉眼看两行字完整、无方块、无半字。

---

## 7. 待办（按优先级）

### P0 — 链路质量（当前最大风险，不是代码问题）

盒子 2.4G 链路极差：**ping 丢包 25%~100%，RSSI -68 ~ -72**，AP 在信道 8 / 40MHz。
所有"偶尔失败"都能追到这上面。代码层面已做到极限（平滑发送 / 8k / 窗口调大），再优化收益很小。

建议（需要人动手，Cursor 改不了）：
1. 盒子挪到离路由器近的地方 / 加个 2.4G 中继
2. 路由器 2.4G 改 **20MHz + 信道 1 / 6 / 11**，别用信道 8

### P1 — WiFi 掉线不自动重连

日志 `WiFi disconnected, reason=201` 之后不自动重连，`wifi_recon` 任务 TCB 分配失败（内部 RAM 碎片）。
影响：盒子静默失联，喊什么都没反应，只能断电重启。

### P2 — 串口调试命令通道不通

`lcd1` / `stat` / `lcdprompt` / `voice` 这些串口命令全无响应（既有问题，非本次引入）。
所以本次全部改用 `POST /cmd` 和 HTTP 接口验证。要修得看 `main.c` 的 `uart_cmd_task`。

### P3 — 体验细节（用户还没拍板）

- 白条高度：现在 40px。可压到 34px（更不挡画面）
- 字号：现在 16px，一行 17 字。放大到 20px 则一行只剩 8 字，**不划算，不建议**
- 单句超过约 1.7 秒会被截尾（`PC_UPLINK_MAX 14000` 的保护）。要支持长句得改成流式分块上传，工程量较大

### P4 — 第二阶段：真正接小智云端

`voice_mode.c` 的 `voice_mode_start_turn_at()` 里已留好唯一分流点，标了 `TODO(第二阶段)`。
设备端交互逻辑不用改，只换后端协议（WebSocket + Opus）。需要额外依赖 `websockets`、`opuslib` + Windows `libopus` DLL。

---

## 8. 排障速查表

| 现象 | 先查哪里 | 典型原因 |
| --- | --- | --- |
| 喊「小电脑」完全没反应，串口无 `命中「小电脑」` | `managed_components/.../commands_cn.txt` 是否 316 行 | 依赖被重拉，词表退回 315 条 → 重跑 `enable_pc_voice.py` + `build_models` |
| 串口有 `命中「小电脑」`，但没后续 | `wake_word.c:233` 返回值 | `pc_voice_start_turn_at` 返回 `INVALID_STATE` = 老人正在对讲，主动让路 |
| 屏幕闪一下黑，没字 | 串口有无 `画横幅` 日志 | 有日志 = 字库缺字 → 跑 `gen_font_extra.py`；无日志 = 任务没起来 |
| 卡在 `发送进度 16384` 不动 | `sdkconfig` 的 `TCP_SND_BUF_DEFAULT` | 还是 5760，或者音频超过 14000 字节 |
| 上传耗时 50 秒以上 | `pc_send_all` 的 smooth 参数 | 传了 false，一次性灌满 lwIP |
| 微信被打开两次 | `PC_ERR_RETRY` / `PC_ERR_GAVE_UP` 判定 | 把"已发出但没回话"误判成可重试 |
| 盒子说"没回话"，但电脑确实执行了 | 电脑端有没有回 100 Continue | `iot_bridge.py:716` 被删了，板子不知道音频已送达 |
| 字幕半句 + 方块 | `PC_REPLY_BUF` 大小 | 太小装不下 URL 编码后的中文（一汉字 9 字节） |
| 白底黄字 / 看不见字 | `lcd_prompt_show_styled` 的 fg 处理 | 又把 `0x0000` 当"没指定"自动选了黄字 |
| 板子重启，`xTaskPriorityDisinherit` | `pc_ensure_init()` | 又把二值信号量换回互斥量了 |
| 识别成 "I wish you" 之类英文 | `pc_downsample_8k()` | 又改成直接抽点了 |
| 烧录失败 | 串口占用 | 关掉 `idf.py monitor` / 串口助手 / 抓包脚本 |

---

## 9. 代码约定（跟着现有风格写，别引入新风格）

1. **注释一律中文**，写"为什么"不写"是什么"。现有代码里大量 `【为什么 xxx】` 段落，改代码时**别删这些注释**——它们记录了踩过的坑。
2. **只追加不修改**：碰老人相关文件时，新代码用 `#ifdef ENABLE_PC_VOICE` 包起来，原有行不动。`enable_pc_voice.py` 就是这么打补丁的。
3. **`ESP_LOGI(TAG, ...)` 的中文要能看懂**：现有日志是给非专业用户看的（"发不动"、"电脑已收到音频"），别改成 `send failed`。
4. **失败要如实报**，绝不能假报成功。连不上就显示"电脑没连上"，不能播"开好了"。
5. **纯 C，不用 C++**；不引 LVGL；能自己画的（横幅）不引库。
6. **内存**：内部 RAM 极紧，>4KB 的缓冲一律 PSRAM；`xTaskCreate` 在开机阶段做，不要运行期建。

---

## 10. 两条历史教训（跟本模块无关，但会咬人）

1. **`D:\Software\kanhu\remote-care-box` 里 `git status` 有一堆 `M`**，很多是历史未提交改动，**不代表是这次改的**。判断"我改了哪些"用：
   ```bat
   git status --short
   git diff --stat
   ```
   本次小电脑相关的新增只有：`firmware/main/pc_voice.c`、`pc_voice.h`、`pc_voice_config.h`、`firmware/tools/*.py` 若干、`firmware/model/fst/commands_cn.txt`。

2. **`xiaozhi-box3` 目录不属于这个仓库**。里面有整片备份、小智固件、NVS 工具。刷小智/切回看护走 `pc_receiver/mode_switch.py`，跟小电脑模块完全无关，别混在一起改。

---

## 附录：本文档涉及的关键常量一览

```c
/* pc_voice.c */
#define PC_SAMPLE_RATE      16000   /* 采集采样率 */
#define PC_UPLINK_RATE      8000    /* 上传采样率（降采样后） */
#define PC_UPLINK_MAX       14000   /* 单次上传硬上限，对应 TCP_SND_BUF 16384 */
#define PC_SILENCE_END_MS   500     /* 静音多久算说完 */
#define PC_MAX_SEC          10      /* 单句最长录音 */
#define PC_RMS_THRESHOLD    350     /* VAD 能量门限 */
#define PC_START_FRAMES     3       /* 连续 3 帧有声才算开口 */
#define PC_LINE_CHARS       17      /* 字幕每行字数 */
#define PC_SUBTITLE_FG      0x0000  /* 黑字 */
#define PC_SUBTITLE_BG      0xFFFF  /* 白底 */
#define PC_TASK_STACK       8192    /* 工作线程栈 */

/* wake_word.h */
#define WAKE_WORD_PC_NAME       "小电脑"
#define WAKE_WORD_PC_PINYIN     "xiao dian nao"
#define WAKE_WORD_PC_CMD_ID     316

/* sdkconfig */
CONFIG_LWIP_TCP_SND_BUF_DEFAULT=16384
CONFIG_LWIP_TCP_WND_DEFAULT=16384
```
