# 电脑端接收脚本

> 远程看护盒子 · PC 侧工具
> 板子固件在 `../firmware/`，需求文档在 `../docs/`

---

## 1. 这里有什么

| 文件 | 作用 | 状态 |
| --- | --- | --- |
| `recv_event.py` | 监听板子主动推来的 TCP 事件流，收到后回执 | ✅ 可用 |
| `talk.py` | 按住说话：采集电脑麦克风 → 灌到设备喇叭 | 🔜 阶段 2 |
| `ble_provision.py` | BLE 配网：手机连设备下发 Wi-Fi 账号密码 | 🔜 阶段 1 |

---

## 2. 快速开始

### 2.1 先看画面（不用任何脚本）

浏览器直接开：

```
http://<板子IP>:8080/
```

板子 IP 在串口日志里（`已连接 Wi-Fi，内网 IP: 192.168.x.x`）。

### 2.2 事件推模式

```powershell
# 起接收端（板子 IP 按实际改）
python recv_event.py --device 192.168.2.138

# 只打印不回执
python recv_event.py --device 192.168.2.138 --no-notify

# 只探一下板子在不在
python recv_event.py --device 192.168.2.138 --probe

# 端口被占就换一个（同时要改固件 camera_pins.h 的 PC_RECEIVER_PORT）
python recv_event.py --device 192.168.2.138 --port 5557
```

正常输出：

```
[22:09:13] 板子在线: IP=192.168.2.138 uptime=2984875s psram=8388608 audio=True
[22:09:13] 就绪。等待板子连接... (Ctrl+C 退出)
[22:09:16] 板子已连接: 192.168.2.138:54321
[22:09:16] [收到] type=button msg='老人按了求助按钮' seq=999001 ts=1793801356312
[22:09:16] 已回执 -> http://192.168.2.138:8080/notify
```

板子串口那边会同时打出 `电脑已确认收到: 老人按了求助按钮` —— 这是闭环的另一半证据。

---

## 3. 拉模式 vs 推模式

需求文档 §3.1.1 定的混合方案，两个都要保留：

```
拉模式 (pull)                        推模式 (push)
─────────────────────────────────    ─────────────────────────────────
浏览器 JS 每 500ms GET /event        板子有事件就连过来推 NDJSON
取走即消失                          事件带 seq，重连可补传
零配置，永远可用（保底）             需要电脑侧起接收脚本
网页关了 = 事件丢失                   网页关了也有人在听
```

关键：**事件带 seq**，网页用 `GET /event?since=N` 去重，所以两种模式同时开着也不会重复弹窗、也不会互相抢事件。

---

## 4. 改文案

文案在**固件**里，不在脚本里：

```c
// firmware/main/camera_pins.h
#define BUTTON_MESSAGE      "老人按了求助按钮"
```

改这一行重新烧录即生效（M2.5 验收项）。

---

## 5. 防火墙

Windows 首次跑会弹「Python 是否允许访问网络」，必须选**允许**，否则板子连不上。

如果被静默拦了，用管理员 PowerShell 放行：

```powershell
New-NetFirewallRule -DisplayName "kanhu-recv-5556" -Direction Inbound -LocalPort 5556 -Protocol TCP -Action Allow
```

---

## 6. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `探不到板子 ... 的 /health` | 板子没上电 / Wi-Fi 没连 / IP 变了 | 看板子串口日志里的当前 IP |
| `监听 5556 失败: address already in use` | 端口被占 | `--port 5557` 并同步改固件 `PC_RECEIVER_PORT` |
| 板子连不上（一直"等待板子连接"） | 防火墙拦了 | 见上一节 |
| 板子连上了但回执失败 | 板子 IP 变了 | `--device` 改成新 IP |
| 收不到事件 | 固件侧 TCP 推送代码还没上线 | 当前固件只有拉模式，用浏览器 `/` 页面看弹窗 |
| 事件重复回执 | 重连后补传 | 已做 seq 去重，同一 seq 只回执一次 |

> ⚠️ **不要用 5555**：本机 5555 已被 Android 模拟器
> （`qemu-system-x86_64`）占用，绑不上会直接 connect 失败。
> 默认用 **5556**。

---

## 7. 依赖

`recv_event.py` 只用 Python 标准库，**不需要 pip install 任何东西**。

```powershell
python --version   # 需要 3.8+
```

`talk.py`（阶段 2）需要：

```powershell
pip install sounddevice numpy
# Windows 还需要 PortAudio: https://github.com/PortAudio/portaudio/releases
```

---

## 8. 手动模拟一个事件（不用按板子）

```powershell
python -c "import socket,json,time; s=socket.create_connection(('127.0.0.1',5556)); s.sendall((json.dumps({'type':'button','msg':'测试事件','seq':1,'ts':int(time.time()*1000)},ensure_ascii=False)+'\n').encode('utf-8')); time.sleep(1)"
```

配合串口命令 `evtest` 一起用，可以把链路拆开逐段验证。
