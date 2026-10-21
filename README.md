# 远程看护盒子

> 面向独居老人的远程看护产品：正点原子 ESP32-S3 BOX 设备端 + 腾讯云后台规则引擎 + Flutter App / 微信小程序。

**对外一句话**：你不在的时候，出事你会知道。

## 文档

- [开发需求文档](docs/开发需求文档.md) —— 完整 PRD + 技术规格 + 里程碑 + 风险表
- [实施计划（PLAN）](docs/实施计划.md) —— 开发顺序与验收要点
- [环境搭建 checklist](docs/环境搭建checklist.md) —— ESP-IDF/PSRAM/Flash 配置逐条可勾选清单

## 技术选型

| 层 | 选型 |
| --- | --- |
| 设备端 | 正点原子 ESP32-S3 BOX（ESP32-S3-WROOM-1-N16R8，OV2640 + ES8311/ES7210），ESP-IDF v5.3.x + ESP-ADF/ESP-SR |
| 音视频 | MJPEG（无 H.264 硬编码）经 WSS 单连接上行，服务端出 FLV/HLS |
| 后台 | 腾讯云 Linux + Docker：SRS 媒体网关 + 业务 API + 规则引擎（Python）+ PostgreSQL/Redis/COS |
| 客户端 | Flutter App（iOS/Android）+ 微信小程序 |

## 目录

```
docs/         需求与协议文档
firmware/     ESP-IDF 设备端工程
server/       服务端（api / rule_engine / media / deploy）
app/          Flutter App
miniprogram/  微信小程序
tools/        事件回放、设备冒烟脚本
```

## 当前进度

- [x] 需求文档 v1.0
- [ ] M0 环境跑通（ESP-IDF 安装、PSRAM Octal + Flash 16MB 配置、COM3 烧录验证）
- [ ] M1 摄像头局域网页面
- [ ] M2 音频对讲（v1 半双工 / v2 全双工 AEC）
- [ ] M3 设备上云推流
- [ ] M4 服务端 + 规则引擎
- [ ] M5 Flutter App
- [ ] M6 微信小程序
- [ ] M7 内容干预 + 周报
- [ ] M8 单家庭试点
