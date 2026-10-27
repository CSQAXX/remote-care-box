# 可行性验证 环境搭建 checklist

> 配套文档：`可行性验证需求文档.md` / `可行性验证实施计划.md`
> 范围：**只覆盖 M0 + M1 + M2**，不含服务端/数据库/小程序/Flutter
> 用法：每完成一项就勾选并写日期。**M0 全部 P0 项打勾才算 M0 通过。**

图例：`[ ]` 未做　`[~]` 进行中　`[x]` 完成　`[!]` 阻塞（写明原因）

---

## P0 — 工具链（不完成无法进 M1）

| # | 项 | 命令 / 动作 | 通过标准 | 状态 | 日期 |
| --- | --- | --- | --- | --- | --- |
| 1.1 | 项目目录就位 | 目录 `D:\Software\kanhu\remote-care-box` | `git log` 有 v1.0 提交，`git fsck` 无损 | [x] | 2026-10-22 |
| 1.2 | 克隆 ESP-IDF v5.3.x | `git clone -b v5.3.2 --recursive https://github.com/espressif/esp-idf.git`（**先清空代理**） | 源码完整 | [ ] | |
| 1.3 | 安装工具链 | `install.ps1 esp32s3` | 无报错 | [ ] | |
| 1.4 | 记录 `IDF_PATH` | `D:\Development Environment\ESP-IDF\esp-idf` | 环境变量已设 | [ ] | |
| 1.5 | 验证 | `idf.py --version` | 输出 v5.3.x | [ ] | |
| 1.6 | 装 esptool | `pip install esptool` | `esptool.py version` | [ ] | |
| 1.7 | 装 Espressif 扩展 | Cursor 扩展市场安装 | 扩展内 `idf.py --version` 有输出 | [ ] | |

> ⚠️ **代理提醒**：本机 `HTTP_PROXY=127.0.0.1:10810` 会导致 git 拉 GitHub 报 `schannel: failed to receive handshake`。
> **每次跑 git 前先清空**：`$env:HTTP_PROXY=""; $env:HTTPS_PROXY=""`

---

## P0 — 硬件探测

| # | 项 | 命令 | 通过标准 | 状态 | 日期 |
| --- | --- | --- | --- | --- | --- |
| 2.1 | 设备在位 | 设备管理器 | `COM3` = `USB\VID_303A&PID_1001&MI_02` | [x] | 2026-10-22 |
| 2.2 | 读 Flash | `esptool.py --chip esp32s3 -p COM3 flash_id` | 识别 16MB | [ ] | |
| 2.3 | 读 MAC | `esptool.py --chip esp32s3 -p COM3 read_mac` | 打印 MAC | [ ] | |
| 2.4 | 读 PSRAM | `printf("%d\n", esp_psram_get_size())` | `8388608` | [ ] | |
| 2.5 | 模组确认 | 看丝印 | N16R8（16MB Flash + 8MB Octal PSRAM） | [ ] | |
| 2.6 | 按键 GPIO 确认 | 查正点原子原理图 / 短接测试 | 记录可用按键与 GPIO | [ ] | |
| 2.7 | Wi-Fi 条件确认 | 问网管/看路由器 | 2.4GHz + WPA2 可见 | [ ] | |

> 2.7 不通过会导致 M1 推不了流。**尽早确认，备好手机热点。**

---

## P0 — sdkconfig 核对（逐项截图存档）

| # | 配置项 | 值 | 备注 | 状态 |
| --- | --- | --- | --- | --- |
| 3.1 | `CONFIG_ESPTOOLPY_FLASHSIZE_16MB` | `y` | | [ ] |
| 3.2 | `CONFIG_ESPTOOLPY_FLASHSIZE` | `"16MB"` | | [ ] |
| 3.3 | `CONFIG_SPIRAM_MODE_OCT` | `y` | **最关键，漏了摄像头直接失败** | [ ] |
| 3.4 | `CONFIG_SPIRAM_SPEED_80M` | `y` | | [ ] |
| 3.5 | `CONFIG_SPIRAM_USE_MALLOC` | `y` | | [ ] |
| 3.6 | `CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL` | `4096` | | [ ] |
| 3.7 | `CONFIG_ESP32S3_BOX_BOARD` | `y` | 正点原子 board config | [ ] |
| 3.8 | `CONFIG_OV2640_SUPPORT` | `y` | | [ ] |
| 3.9 | 固化为 `sdkconfig.defaults` | 存到 `firmware/` | 3.1~3.8 全过后再做 | [ ] |

---

## P0 — 烧录验证

| # | 项 | 命令 | 通过标准 | 状态 |
| --- | --- | --- | --- | --- |
| 4.1 | 建工程 | `idf.py -C firmware set-target esp32s3` | 生成 `sdkconfig` | [ ] |
| 4.2 | hello_world 编译 | `idf.py -C firmware build` | 成功 | [ ] |
| 4.3 | 烧录 | `idf.py -C firmware -p COM3 flash monitor` | 打印 `Hello world!` | [ ] |
| 4.4 | PSRAM 读数 | 加 printf | `8388608` | [ ] |

---

## P1 — M1 摄像头出图

| # | 项 | 通过标准 | 状态 |
| --- | --- | --- | --- |
| 5.1 | 拿到正点原子 camera 例程源码 | 能看清 CMakeLists 与 main.c | [ ] |
| 5.2 | 确认例程要求的 IDF 版本 | 与 v5.3.x 匹配 | [ ] |
| 5.3 | **例程原样编译通过** | `idf.py build` 无错 | [ ] |
| 5.4 | 例程原样烧录 + monitor | 摄像头初始化成功，无 `0x105` | [ ] |
| 5.5 | **浏览器出图** | 看到画面（截图存档） | [ ] |
| 5.6 | 改办公室 Wi-Fi | 串口打印内网 IP | [ ] |
| 5.7 | 访问 `http://<板子IP>:8080/` | 画面持续刷新 | [ ] |
| 5.8 | 画质确认 | ≥320x240、≥10fps | [ ] |
| 5.9 | 稳定性 | 连跑 8 小时不重启 | [ ] |
| 5.10 | IP 漂移解决 | mDNS 或 DHCP 静态绑定 | [ ] |

---

## P1 — M2 按键弹话

| # | 项 | 通过标准 | 状态 |
| --- | --- | --- | --- |
| 6.1 | 固件 `btn_task` | 短按触发一次，日志可见 | [ ] |
| 6.2 | `GET /event` 接口 | 返回 JSON，无事件时 `{"type":"none"}` | [ ] |
| 6.3 | 网页 500ms 轮询 | 能拉到事件 | [ ] |
| 6.4 | **按按钮网页弹出中文** | 截图/录屏存档 | [ ] |
| 6.5 | 文案可配置 | 改一行常量重烧即生效 | [ ] |
| 6.6 | （选做）TCP 推送到电脑 `:5555` | Python 脚本收到 | [ ] |
| 6.7 | （选做）板载 LCD + 蜂鸣 | 能看见听见 | [ ] |

---

## P2 — 电脑端（已具备的可以跳过）

| # | 项 | 状态 | 日期 |
| --- | --- | --- | --- |
| 7.1 | Python 3.12.10 可用 | [x] | 2026-10-22 |
| 7.2 | Chrome / Edge 可用 | [x] | — |
| 7.3 | 端口 5555 / 8080 未被占用 | [ ] | |
| 7.4 | 电脑与板子同一网段可 ping 通 | [ ] | |

> ❌ 本阶段**不需要**：Node.js、MySQL、Docker、Flutter、微信开发者工具、腾讯云、PostgreSQL、Redis。
> 这些在 v1.0 里，M3 评估后再决定要不要装。

---

## 常见报错对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `Camera init failed with error 0x105` | PSRAM 未开或非 Octal | 3.3 / 3.5 / 3.6 |
| `Failed to get frame` | 摄像头供电/排线/引脚 | 检查排线方向、board config、供电 |
| `Flash size mismatch` | flash 配置不是 16MB | 3.1 / 3.2 |
| `PSRAM ID read error` | 模组非 N8R8 或频率过高 | 降到 40M 验证；核对丝印 |
| `Port COM3 in use` | 串口被占 | 关闭 monitor / Arduino / 串口助手 |
| `Invalid head of stream` | 镜像与芯片不匹配 | `idf.py erase-flash` 后重刷 |
| **`schannel: failed to receive handshake`** | **系统代理拦截 git** | **清空 `HTTP_PROXY`/`HTTPS_PROXY`** |
| `unexpected eof while reading` | 同上（openssl 后端） | 同上 |
| `No such file or directory: sdkconfig` | 未 set-target | `idf.py set-target esp32s3` |
| `wifi:sta_connect(): fail` / 一直重连 | 5G 网络 / WPA3 / 信号弱 | 开 2.4G 兼容；换热点；拉近天线 |
| 浏览器一直转圈不出图 | 板子没起 server / IP 变了 / 被防火墙拦 | 看串口有没有打印 URL；核对 IP；Windows 防火墙放行 |
| 画面卡成幻灯片 | 带宽不足 | 降分辨率 320x240；降帧率 5fps |
| 按键没反应 | GPIO 配错 / 没上拉 / 消抖 | 查原理图；短接 GPIO-GND 验证；补 `GPIO_PULLUP` |
| 页面刷新一次变两个连接 | 旧连接没关 | 页面用 `img.onload` 重连；服务端限一个客户端 |

---

---

## 性能与花屏专项（2026-10-27）

### 背景

M1/M2 打通后, 出现两个问题: 画面**一闪一闪** (花屏), 帧率只有 6.4 fps。

### 根因与修复

| 问题 | 根因 | 修复 | 验证 |
| --- | --- | --- | --- |
| 画面撕裂/花屏 | `fb_count=2`, 摄像头连续 DMA 写同一块缓冲, 应用读的时候下一帧已经覆盖上来 | `fb_count` 2 -> **4**, `grab_mode` 改 `CAMERA_GRAB_LATEST` | 撕裂帧计数 **6/350 -> 0/800** |
| 画面闪烁 | JPEG 编码一帧 72ms 纯 CPU, 编码任务优先级高于 LCD 刷新, 把 LCD 饿死 | 自写 `jpeg_yield.cpp`: 编码每 16 行让出一次 CPU; LCD 任务优先级提到编码之上 | 待烧录复测 |
| 帧率低 (6.4fps) | 1) 编译是 `-Og` (DEBUG) 2) 每帧 `frame2jpg` malloc 128KB 3) LCD 和视频流各编各的 | 1) 改 `CONFIG_COMPILER_OPTIMIZATION_PERF` (-O2) 2) 改流式回调, 零额外拷贝 3) 编码统一到生产者, 多客户端共用 | **6.4 -> 23.8 fps** |
| 看门狗报警 | 编码任务连续 70ms 不让出 CPU | 拆成独立编码任务 + 编码中让出 | 36 次 -> **0 次** |
| 正常模式摄像头没初始化 | `camera_init()` 只在诊断模式调用过 | 正常模式补上 | — |

### 架构变更

原来 LCD 预览和 MJPEG 视频流各自调 `esp_camera_fb_get()`, 两边抢同一个摄像头,
而且各编一遍 JPEG (CPU 直接翻倍)。现在改成生产者-消费者:

```
cam_producer (prio 6)  取帧 -> 拷进 RGB565 槽位 -> 投递给编码队列
    |                                    |
    +-> lcd_slots (给 LCD 预览, prio 4)   +-> jpeg_enc (prio 3) -> jpg_slots -> MJPEG HTTP
```

关键点:
- 帧槽位按用途分组 (`s_rgb_slots` / `s_lcd_slots` / `s_jpg_slots`),
  因为 JPEG 编码任务占用槽位 72ms, 共用会导致 LCD 永远抢不到 -> 屏幕不刷新
- 槽位策略是"最新帧优先": 旧帧直接丢弃, 不排队 (直播场景旧帧没价值)

### 关键配置改动

```ini
# sdkconfig.defaults
CONFIG_COMPILER_OPTIMIZATION_PERF=y      # 原来是 DEBUG (-Og), JPEG 编码慢好几倍
```

```c
// main.c
#define WIFI_ENABLE          1   # Wi-Fi 总开关, 排障时可关掉做对照实验
#define LCD_PREVIEW_ENABLE   1   # LCD 预览开关, 排障时可关掉做对照实验
#define JPEG_QUALITY        65   # 原来是 12 (jpge 是 1-100 越大越清晰, 12 等于最低质量)
#define JPEG_YIELD_EVERY_LINES 16 # 编码每多少行让出一次 CPU
#define LCD_PREVIEW_MS      500   # LCD 预览 ~2fps, 把 CPU 让给视频流
```

### 定位花屏的方法论（重要）

之前的排查一直靠肉眼看, 分不清是"摄像头数据坏了"还是"显示出了问题"。
这次加了两个**数字化**手段, 以后查类似问题都该这么干:

1. **撕裂自动检测**: 拷贝帧缓冲前后各算一次 CRC, 不一致 = 这一帧在拷贝期间被 DMA 改写了。
   计数打出来, 就不用盯着屏幕猜。
2. **逐项对照实验**: 用编译期开关 (`WIFI_ENABLE` / `LCD_PREVIEW_ENABLE`) 逐个关掉功能,
   一次只变一个变量, 看现象怎么变。这次就是靠"关掉 JPEG 编码 -> 不闪了"定位到真凶的。

### 坑位记录

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `reason=201` 一直连不上 Wi-Fi | 办公室 Wi-Fi 是 **5GHz** (信道 52), ESP32-S3 只支持 2.4GHz | 用 2.4G 热点; 或板子开 AP 模式 |
| `reason=210` 握手超时 | 密码错, 或 AP 是 WPA2-企业认证 (ESP32 不支持 PEAP) | 换成 WPA2-PSK 个人版 |
| `task_wdt: CPU 1: xxx` | 任务连续太久不让出 CPU | 拆任务 + 编码中 yield |
| 编译报 `format '%u' expects unsigned int` | Xtensa 上 `uint32_t` 是 `long unsigned int` | 用 `PRIu32`, 不要 `(unsigned)` 强转 |
| `WinError 6` 编译中断 | 收尾阶段文件句柄失效 (Windows) | 重跑一次增量构建即可 |

---

## 结论记录

- M0 实际耗时：____
- M1 实际耗时：____
- M2 实际耗时：____
- 阻塞项：____
- M3 评估结论：☐ 走 A（做完整产品）　☐ 走 B（做三件套交付）　☐ 走 C（假设不成立）
- 备注：____
