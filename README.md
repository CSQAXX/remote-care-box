# 远程看护盒子

> 正点原子 ESP32-S3 BOX 远程看护项目

## ⚠️ 当前阶段：可行性验证（只做摄像头出图 + 按键）

> **本阶段只做一件事：证明这块板子的画面能在浏览器里看到，并且能被按键触发交互。**
>
> 目标达成后，再决定要不要投入做完整产品。

### 现在要做的（M0 → M1 → M2 → M3）

| 里程碑 | 内容 | 通过标准 |
| --- | --- | --- |
| **M0** | ESP-IDF 装好、烧录通、PSRAM 读数对 | monitor 打印 `Hello world!`，PSRAM = 8388608 |
| **M1** | 正点原子 camera 例程跑通，浏览器出图 | **浏览器能看到实时画面** |
| **M2** | 按板子按钮，网页弹出中文提示 | **按一下，网页弹出一句话** |
| **M3** | 整理、录演示、评估决策 | 决定下一步走哪条路 |

### 本阶段明确不做

❌ 腾讯云 / 域名 / 备案　❌ 业务 API 与数据库　❌ 规则引擎　❌ Flutter App　❌ 微信小程序　❌ **双向对讲**　❌ OTA　❌ 内容干预

> **技术上也不要碰**：本阶段不上公网，局域网内跑通就够。

---

## 文档

### 当前执行（看这三份就够）

- [可行性验证需求文档](docs/可行性验证需求文档.md) —— 当前唯一执行依据
- [可行性验证实施计划](docs/可行性验证实施计划.md) —— 怎么一步步做
- [可行性验证环境checklist](docs/可行性验证环境checklist.md) —— 逐条可勾选清单 + 报错对照表

### 暂缓执行（v1.0 完整产品蓝图）

- [开发需求文档 v1.0](docs/开发需求文档.md) —— 完整 PRD + 技术规格，**M3 评估后再决定是否启动**
- [实施计划 v1.0](docs/实施计划.md)
- [环境搭建checklist v1.0](docs/环境搭建checklist.md)

> ⚠️ 两套文档冲突时，**以"可行性验证"系列为准**。

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
firmware/        ESP-IDF 固件工程（M0 建）
pc_receiver/     电脑端接收脚本与网页（M1/M2 建）
tools/           冒烟测试脚本
app/ server/ miniprogram/   ← v1.0 占位空目录，本阶段不用，可自行删除
```

## 环境

| 组件 | 位置 | 状态 |
| --- | --- | --- |
| ESP-IDF v5.3.x | `D:\Development Environment\ESP-IDF\esp-idf` | 待装 |
| 工具链 | `D:\Development Environment\ESP-IDF\tools` | 待装 |
| esptool | pip 全局 | 待装 |
| Cursor 扩展 | Espressif IDF | 待装 |
| Python | `D:\software\python\Python312` | ✅ 已有 |
| Chrome / Edge | 系统自带 | ✅ 已有 |

> ⚠️ **代理坑**：本机 `HTTP_PROXY=127.0.0.1:10810` 会让 git 拉 GitHub 失败。
> 跑 git 前先清空：`$env:HTTP_PROXY=""; $env:HTTPS_PROXY=""`
