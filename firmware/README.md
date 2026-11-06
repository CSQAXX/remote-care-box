# 固件工程 (ESP-IDF)

> 远程看护盒子 设备端固件
> 硬件：正点原子 ATK-DNESP32S3-BOX3 (ESP32-S3-WROOM-1-N16R8, 16MB Flash / 8MB Octal PSRAM)

配套文档：
- [可行性验证需求文档](../docs/可行性验证需求文档.md) —— 当前执行依据
- [开发需求文档 v1.0](../docs/开发需求文档.md) —— 完整产品需求
- [音频子系统验证记录](../docs/音频子系统验证记录.md) —— 音频链路实测证据

---

## 1. 环境

| 组件 | 位置 | 说明 |
| --- | --- | --- |
| ESP-IDF | `D:\Development Environment\ESP-IDF\esp-idf` | v5.3.5 |
| 工具链 | `D:\Development Environment\ESP-IDF\tools` | riscv32-esp-elf 13.2.0 |
| 串口 | `COM3` | `USB\VID_303A&PID_1001\MI_02` (USB JTAG/serial) |
| Python | `D:\software\python\Python312` | 电脑端脚本用 |

### 每次开终端先加载环境

```powershell
& "D:\Development Environment\ESP-IDF\esp-idf\export.ps1"
```

或者在 cmd 里：

```bat
call "D:\Development Environment\ESP-IDF\esp-idf\export.bat"
```

---

## 2. 编译 / 烧录 / 监视

```powershell
# 编译
idf.py -C firmware build

# 烧录 + 看日志（Ctrl+] 退出 monitor）
idf.py -C firmware -p COM3 flash monitor

# 只烧录不监视
idf.py -C firmware -p COM3 flash

# 擦除整片（换固件结构/怀疑 Flash 脏了时）
idf.py -C firmware -p COM3 erase-flash
```

> ⚠️ **COM3 被占**：烧录前关掉所有串口监视器（Arduino IDE / 串口助手 / 上一个 `idf.py monitor`）。这是本项目最高频的报错。
>
> ⚠️ **代理坑**：拉外部依赖（组件管理器、git）前先清空代理：
> ```powershell
> $env:HTTP_PROXY=""; $env:HTTPS_PROXY=""
> ```

---

## 3. 配置改法

### 3.1 sdkconfig 八项（已固化在 `sdkconfig.defaults`）

| 配置项 | 值 | 漏配后果 |
| --- | --- | --- |
| `CONFIG_ESPTOOLPY_FLASHSIZE_16MB` | `y` | Flash size mismatch |
| `CONFIG_SPIRAM_MODE_OCT` | `y` | **摄像头 init 失败 0x105** |
| `CONFIG_SPIRAM_SPEED_80M` | `80M` | 帧率腰斩 |
| `CONFIG_SPIRAM_USE_MALLOC` | `y` | 帧缓冲分配失败 / 全黑 |
| `CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL` | `4096` | 内部 RAM 不足 |
| `CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240` | `240` | JPEG 编码慢 3~4 倍 |
| `CONFIG_ESP32S3_DATA_CACHE_64KB` | `y` | PSRAM 带宽不够 |
| `CONFIG_CODEC_ES8311_SUPPORT` / `CONFIG_CODEC_ES7210_SUPPORT` | `y` | 音频不可用 |

另外两个必须开的：

```
CONFIG_COMPILER_OPTIMIZATION_PERF=y     # JPEG 编码性能的关键 (-O2)
CONFIG_CAMERA_PSRAM_DMA=y              # 开 Wi-Fi 时不丢行
CONFIG_SCCB_HARDWARE_I2C_DRIVER_LEGACY=y  # 和 AW9523B 共用 I2C0
CONFIG_CODEC_I2C_BACKWARD_COMPATIBLE=y    # 同上
```

### 3.2 改 Wi-Fi / 文案 —— `main/camera_pins.h`

```c
#define WIFI_SSID           "501"
#define WIFI_PASSWORD       "88378389"
#define WIFI_MODE_AP        0     /* 1=板子开热点, 0=板子连路由器 */

#define BUTTON_MESSAGE      "老人按了求助按钮"   /* 网页弹出的文案, 改这一行 */

#define PC_RECEIVER_IP      "192.168.2.135"      /* 电脑端接收服务地址 */
#define PC_RECEIVER_PORT    5556                 /* 注意别用 5555 */
```

> ⚠️ **SSID 填错 = 板子疯狂重连 = 抢占 GDMA 总线 = 摄像头 DMA 抢不到带宽 = 花屏。**
> 换网络必须同步改这里。2.4GHz / 5GHz 可能是两套不同的 BSSID 和认证方式，
> ESP32-S3 只支持 2.4GHz。

### 3.3 改帧率 / 画质 —— `main/main.c`

```c
#define JPEG_QUALITY    65      /* 质量越高 -> 越大 -> 越慢 */
#define LCD_PREVIEW_MS  200     /* LCD 本地预览间隔 (约 2 fps) */
```

当前实测：320x240 @ **7.5fps**（需求文档要 10fps，已评估接受）。

---

## 4. HTTP 端点

板子起好后连上 Wi-Fi，日志里会打印内网 IP。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 带 `<img>` 的页面，同时 500ms 轮询 `/event` |
| GET | `/stream` | MJPEG 实时画面 (`multipart/x-mixed-replace`) |
| GET | `/event` | 取一个按键事件 (JSON)，无事件时 `{"type":"none"}` |
| POST | `/notify` | 电脑确认收到（body 是文案） |
| GET | `/health` | 状态 JSON（ip/uptime/frames/psram/audio_ready/...） |
| GET | `/raw` | 导出原始 RGB565 帧（取证用）；`?capture=1` 先等新帧 |
| GET | `/audio` | 原始 PCM 流（调试） |
| GET | `/audio/alaw` | 上行 G.711 A-law 16k 单声道裸字节流 |
| POST | `/audio/down` | 下行注入 PCM16 16k 单声道，立即播放 |
| GET | `/audio/level` | 当前录音电平 JSON |

浏览器直接访问：

```
http://<板子IP>:8080/
```

---

## 5. 串口命令速查

> 命令在 `idf.py monitor` 里输入，回车执行。分隔符是**空格**，不是 tab。
> ⚠️ `uart_cmd_task` 用 `read(STDIN_FILENO)`，会和 IDF console 抢 stdin，
> 某些情况下命令不生效 —— 改用 `esp_console_cmd_register` 是待办项。

### 5.1 常用

| 命令 | 作用 |
| --- | --- |
| `stat` | 打印视频统计（帧数 / JPEG 字节 / TORN 计数） |
| `scan` | 扫描 Wi-Fi，列出 SSID/信道/信号/认证方式 |
| `wifix` | 重启 Wi-Fi（连不上时用） |
| `wifioff` | 关闭 Wi-Fi（纯本地对照用） |
| `q` | 退出 monitor |

### 5.2 按键与事件

| 命令 | 作用 |
| --- | --- |
| `evtest` | 注入一条默认文案的看护事件 |
| `evtest <自定义文案>` | 注入自定义文案（验证文案可配置） |
| `btnraw` | 打印 GPIO0 / XIO0 / XIO1 原始电平 |
| `btnraw 30` | 连续打印 30 秒 |
| `bootpress` | 模拟按一下 BOOT 键（驱动 GPIO0 高→低→高） |
| `keytest` | 临时把 K1/K2 驱动为推挽输出高，区分"硬短地"和"缺上拉" |
| `awregs` | 打印 AW9523B 的 INPUT/OUTPUT/CONFIG/CTL/LED 寄存器 |

### 5.3 音频

| 命令 | 作用 |
| --- | --- |
| `tone` | 开关循环 440Hz 测试音（toggle） |
| `astat` | 打印音频统计（peak/rms/是否有人声） |
| `astop` | 停掉所有音频后台任务 |
| `axtest` | 音频自环（板子自己连自己，验证 G.711 上下行） |
| `vol <0-100>` | 设置输出音量 |
| `mgain <dB>` | 设置麦克风增益 |
| `cgain <dB>` | 设置耳机/输出增益 |
| `adcreg` | 打印 ADC 寄存器 |

### 5.4 LCD / 显示

| 命令 | 作用 |
| --- | --- |
| `lcd0` | LCD 显示摄像头画面（正常模式） |
| `lcd1` | LCD 显示固定测试图样 |
| `lcd2` | LCD 显示合成渐变图 |
| `ldirect` | 切换直绘 / 拷贝到槽位再画（A/B 对照） |

### 5.5 摄像头排障（花屏取证用）

| 命令 | 作用 |
| --- | --- |
| `rawdump` | 导出一帧原始 RGB565 到串口（二进制） |
| `regs` | 打印摄像头寄存器 |
| `wr <reg> <val>` | 写摄像头寄存器 |
| `conv <0\|1>` | 切换硬件 YUV422→RGB565 转换器 |
| `pclk <n>` | 设置摄像头 PCLK 分频 |
| `pda <0\|1>` | 切换 PSRAM 直写模式 |
| `mode <n>` | 设置帧格式 |
| `noise` | 开关噪点行统计 |
| `cbar <n>` | 彩条测试 |
| `hash` | 帧哈希对比 |
| `gfix` / `gfixstat` | 灰度修复开关 / 状态 |

---

## 6. 目录结构

```
firmware/
├── main/
│   ├── main.c              # 主逻辑：相机/HTTP/按键/串口命令 (4600+ 行)
│   ├── camera_pins.h       # 引脚定义 + Wi-Fi + 文案配置
│   ├── audio_board.c/.h    # 音频子系统（ES8311 播放 + ES7210 录音）
│   ├── lcd_st7789.c/.h     # LCD 驱动（SPI DMA + 双缓冲）
│   ├── lcd_prompt.c/.h     # 屏幕提示层（§3.8 防惊吓机制）
│   ├── lcd_text.c/.h       # 中文点阵字库渲染
│   ├── lcd_font.c/.h       # 字库数据（自动生成，149 字形）
│   ├── aw9523b.c/.h        # IO 扩展器（摄像头供电/复位 + K1/K2 按键）
│   ├── g711.c/.h           # G.711 A-law 编解码
│   ├── jpeg_yield.cpp/.h   # JPEG 编码分片让出（防 WDT 复位）
│   ├── jpge.h              # 软件 JPEG 编码器
│   └── components/         # v1.0 规划中的模块（目前只有 .gitkeep）
├── sdkconfig.defaults      # 八项配置固化
├── partitions.csv          # 分区表（factory 3MB）
├── tools_diag/             # 排障期诊断脚本（不参与编译）
│   └── frames/             # 取证抓的原始帧（.gitignore 排除）
└── build/                  # 编译产物（.gitignore 排除）
```

---

## 7. 常见报错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `Port COM3 in use` | 串口被别的程序占着 | 关掉所有串口监视器 |
| `Camera init failed with error 0x105` | PSRAM 没开或非 Octal | 核对 `sdkconfig.defaults` 那八项 |
| `Flash size mismatch` | flash 配置不是 16MB | `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y` |
| 画面花屏 / 一闪一闪 | 多半是 Wi-Fi 疯狂重连抢占 GDMA | 核对 SSID；跑 `scan` 看信号；确认没在 5GHz 上重连 |
| 画面撕裂（浏览器端） | 任务分散在不同核，读到陈旧 PSRAM 缓存 | 全部任务钉 `APP_CORE_ID`（core 0） |
| `stack overflow in task main` | 内部 RAM 紧张 | 任务栈从 PSRAM 分配（`task_start` 模式） |
| `Task watchdog got triggered` | 任务饿死 | 检查是否有阻塞调用在 httpd 任务里 |
| 麦克风时好时坏 | I2S RX 多消费者抢 DMA | 单一读者架构（采集任务独占 RX） |
| 喇叭没声音 | codec 打开顺序错 | **必须先 ES7210(录音) 再 ES8311(播放)** |
| git 拉不下来 | 系统代理拦截 | 先 `$env:HTTP_PROXY=""; $env:HTTPS_PROXY=""` |

---

## 8. 已踩过的坑（别再踩）

1. **`httpd_resp_send()` 不能用来发 multipart 响应头** —— 它会自己拼一整套 HTTP 头，把你传的字符串当正文。要用 `httpd_resp_set_type()` + `httpd_resp_send_chunk()`。
2. **PSRAM 缓存每核独立** —— 跨核读会拿到陈旧数据。所有任务钉在同一核。
3. **`pdMS_TO_TICKS(1)` 在 tick=100Hz 时等于 0** —— 让出等于没让。
4. **K2 键 (XIO1) 板上没有外部上拉** —— 浮空读低，必须先观察到"松开"才认按键（armed 逻辑）。
5. **AW9523B 没有内部上拉寄存器** —— P0 默认开漏，要推挽得写 CTL(0x11) 的 bit4。
6. **BOOT 是 strapping 引脚** —— 模拟按下要先拉高 100ms 稳定再拉低，否则会触发芯片复位。
