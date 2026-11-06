# 远程看护盒子

> 正点原子 ESP32-S3 BOX 远程看护项目

## ✅ 可行性验证已完成 → 转入 v1.0 完整产品开发

> **可行性验证 M0→M3 全部完成，技术假设成立。**
>
> 结论：画面能出且稳定（花屏根因已定位修复），按键弹话全链路通。
> 现在按 [开发需求文档 v1.0](docs/开发需求文档.md) 的 M0→M8 做完整产品。

### 可行性验证结论（M0 → M1 → M2 → M3）

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| **M0** | ESP-IDF 装好、烧录通、PSRAM 读数对 | ✅ PSRAM = 8388608 |
| **M1** | 摄像头出图，浏览器看实时画面 | ✅ 320x240 @ 7.5fps，花屏已修复 |
| **M2** | 按板子按钮，网页弹出中文提示 | ✅ 全链路实测通过 |
| **M3** | 整理、文档、评估决策 | ✅ 假设成立 → 转 v1.0 |

### v1.0 待补齐的四块

| 块 | 内容 | 对应需求 |
| --- | --- | --- |
| **设备端** | 活动检测、屏幕提示闭环、可靠性、BLE 配网 | v1.0 §3.5 / §3.8 / §3.9 / §3.10 |
| **PC 端** | 半双工「按住说话」闭环 | v1.0 §3.7 |
| **云端** | WSS 上云、服务端 API、规则引擎 | v1.0 M3 / M4 |
| **软件端** | Flutter App 首页三卡片 + 告警闭环 | v1.0 M5 |

---

## 文档

### 当前执行

- [开发需求文档 v1.0](docs/开发需求文档.md) —— **主需求文档**，完整 PRD + 技术规格
- [实施计划 v1.0](docs/实施计划.md) —— M0→M8 里程碑与验收
- [音频子系统验证记录](docs/音频子系统验证记录.md) —— 音频链路实测证据

### 已完成的可行性验证系列（存档参考）

- [可行性验证需求文档](docs/可行性验证需求文档.md)
- [可行性验证实施计划](docs/可行性验证实施计划.md)
- [可行性验证环境checklist](docs/可行性验证环境checklist.md)
- [固件工程说明](firmware/README.md) —— 编译/烧录/串口命令速查
- [电脑端脚本说明](pc_receiver/README.md)

---

## 技术选型（本阶段）

| 层 | 选型 |
| --- | --- |
| 设备端 | 正点原子 ESP32-S3 BOX（ESP32-S3-WROOM-1-N16R8，OV2640），ESP-IDF v5.3.x |
| 画面协议 | HTTP MJPEG（`multipart/x-mixed-replace`），320x240 @ ~10fps，S3 无 H.264 硬编 |
| 电脑端 | 浏览器 + Python 3.12 接收脚本 |
| 网络 | 办公室 2.4GHz Wi-Fi，**局域网内，不上公网** |
| 数据存储 | 无 |

## 目录

```
docs/            需求与计划文档
firmware/        ESP-IDF 固件工程（见 firmware/README.md）
pc_receiver/     电脑端接收脚本（事件推模式 / 对讲 / BLE 配网）
tools/           冒烟测试脚本 + 事件回放工具
app/ server/ miniprogram/   ← v1.0 完整产品阶段启动
```

## 环境

| 组件 | 位置 | 状态 |
| --- | --- | --- |
| ESP-IDF v5.3.x | `D:\Development Environment\ESP-IDF\esp-idf` | ✅ v5.3.5 已装 |
| 工具链 | `D:\Development Environment\ESP-IDF\tools` | ✅ riscv32-esp-elf 13.2.0 |
| esptool | pip 全局 | ✅ 已装 (v4.12.0) |
| Cursor 扩展 | Espressif IDF | ✅ 已装 |
| Python | `D:\software\python\Python312` | ✅ 已有 |
| Chrome / Edge | 系统自带 | ✅ 已有 |
| Docker Desktop | 系统自带 | ✅ 已有（v1.0 服务端用） |
| Node.js | — | ⬜ 未装（v1.0 暂用 FastAPI，不需要） |
| Flutter SDK | — | ⬜ 未装（v1.0 M5 阶段需要） |

> ⚠️ **代理坑**：本机 `HTTP_PROXY=127.0.0.1:10810` 会让 git 拉 GitHub 失败。
> 跑 git 前先清空：`$env:HTTP_PROXY=""; $env:HTTPS_PROXY=""`
