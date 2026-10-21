# 环境搭建 checklist

> 用法：每完成一项就勾选并备注日期。M0 验收要求全部 P0 项为 ☑。

图例：`[ ]` 未做　`[~]` 进行中　`[x]` 完成　`[!]` 阻塞（写明原因）

---

## P0 固件环境（不完成无法进 M1）

### 工具链
- [ ] 1.1 安装 ESP-IDF v5.3.x（`github.com/espressif/idf-installer` → Windows 安装器 → 全屏安装）
- [ ] 1.2 记录 `IDF_PATH`（默认 `%USERPROFILE%\esp\esp-idf`）
- [ ] 1.3 安装 Espressif IDF 扩展（Cursor/VSCode），指定 IDF 路径与工具链路径
- [ ] 1.4 `pip install esptool`
- [ ] 1.5 `idf.py --version` 正常输出
- [ ] 1.6 `idf.py set-target esp32s3` 生成 sdkconfig

### 硬件探测
- [ ] 2.1 设备出现在 `COM3`（`USB\VID_303A&PID_1001\MI_02`）
- [ ] 2.2 `esptool.py --chip esp32s3 -p COM3 flash_id` → 识别 16MB Flash
- [ ] 2.3 `esptool.py --chip esp32s3 -p COM3 read_mac` → 打印 MAC
- [ ] 2.4 `esp_psram_get_size()` 返回 8388608（8MB）
- [ ] 2.5 确认模组丝印为 N16R8（16MB Flash + 8MB Octal PSRAM）
- [ ] 2.6 拆开外壳确认音频 codec 型号（ES8311 + ES7210？还是 ES8388？）

### sdkconfig 核对（逐项截图存档）
- [ ] 3.1 `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`
- [ ] 3.2 `CONFIG_ESPTOOLPY_FLASHSIZE="16MB"`
- [ ] 3.3 `CONFIG_SPIRAM_MODE_OCT=y`　← **最关键，漏了摄像头初始化直接失败**
- [ ] 3.4 `CONFIG_SPIRAM_SPEED_80M=y`
- [ ] 3.5 `CONFIG_SPIRAM_USE_MALLOC=y`
- [ ] 3.6 `CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL=4096`
- [ ] 3.7 `CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240=240`
- [ ] 3.8 `CONFIG_ESP32S3_DATA_CACHE_64KB=y`
- [ ] 3.9 `CONFIG_OV2640_SUPPORT=y`
- [ ] 3.10 `CONFIG_VIDEO_FB_SIZE_IN_RAW` 正确
- [ ] 3.11 `CONFIG_ESP32S3_BOX_BOARD=y`（board config）
- [ ] 3.12 确认后可将配置固化为 `firmware/sdkconfig.defaults`

### 烧录验证
- [ ] 4.1 `idf.py -p COM3 build flash monitor` 成功打印 Hello world
- [ ] 4.2 正点原子 Camera 例程编译通过
- [ ] 4.3 正点原子 Camera 例程浏览器出图
- [ ] 4.4 正点原子 Audio 例程编译通过
- [ ] 4.5 正点原子 Display 例程编译通过

### ESP-ADF / ESP-SR
- [ ] 5.1 拉取 adf 仓库，BOX audio 例程编译通过
- [ ] 5.2 拉取 sr 仓库，AEC3 管线可运行
- [ ] 5.3 确认 ES8311/ES7210 引脚与 board config 一致

## P1 服务端与客户端

### 服务端
- [ ] 6.1 安装 Node.js LTS（若选 NestJS）或确认 Python 环境（若选 FastAPI）
- [ ] 6.2 安装 Docker Desktop
- [ ] 6.3 PostgreSQL 本地实例
- [ ] 6.4 Redis 本地实例
- [ ] 6.5 腾讯云服务器可 SSH 登录，公网 IP 已确认
- [ ] 6.6 域名已解析并完成备案
- [ ] 6.7 SSL 证书已部署
- [ ] 6.8 微信小程序 AppID 已申请
- [ ] 6.9 小程序服务器域名已在后台配置
- [ ] 6.10 `live-player` 组件权限确认（是否需要直播类目资质）

### 客户端
- [ ] 7.1 Flutter SDK 安装，`flutter doctor` 全绿
- [ ] 7.2 微信开发者工具安装
- [ ] 7.3 Android 设备开启 USB 调试
- [ ] 7.4 iOS 开发者账号（Mac + Xcode）

## 常见报错对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `Camera init failed with error 0x105` | PSRAM 未开或非 Octal | 3.3 / 3.5 / 3.6 |
| `Failed to get frame` | 摄像头供电/排线/引脚 | 检查排线方向、board config、供电 |
| `Flash size mismatch` | flash 配置不是 16MB | 3.1 / 3.2 |
| `PSRAM ID read error` | 模组非 N8R8 或频率过高 | 降到 40M 验证；核对模组丝印 |
| `Port COM3 in use` | 串口被占用 | 关闭 `idf.py monitor` / Arduino 串口监视器 |
| `Invalid head of stream` | 镜像与芯片不匹配 | `idf.py erase-flash` 后重刷 |
| `guru Meditation` in audio task | I2S 缓冲不足 / DMA 冲突 | 增大 DMA 缓冲、核对 ES8311 引脚 |
| `No such file or directory: sdkconfig` | 未执行 set-target | `idf.py set-target esp32s3` |
| 分区表溢出 | 应用过大 | 自定义 `partitions.csv`，预留 OTA 双分区 |

## 结论记录

- 实际 M0 耗时：____
- 阻塞项：____
- 备注：____
