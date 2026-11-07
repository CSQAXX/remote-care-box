# PC 端简易控制台

> 远程看护盒子 · 子女/家属侧的查看与对讲界面

一个单文件、零第三方依赖的 Python 程序，浏览器打开即用。

```
python pc_console.py --device 192.168.2.138 --port 8899
```

打开 <http://127.0.0.1:8899>

---

## 1. 界面三块

| 卡片 | 回答的问题 | 数据来源 |
| --- | --- | --- |
| ① 今天动了没有 | 老人今天还活动吗 | `/activity` 的 `last_active_ts` / `motion` / `human_present` |
| ② 有事没事 | 设备正常吗、告警了吗 | `/health` 全量状态 + `/event` 求助事件弹窗 |
| ③ 点一下就能看 | 现在画面如何、能不能喊话 | `/stream` MJPEG + 按住说话对讲 |

## 2. 依赖

`pc_console.py` 只用 Python 标准库，**不需要 pip install 任何东西**。

- Python 3.8+（3.12 实测通过）
- 老人语音录制用到 `audioop`（Python 3.13 已移除）。若在 3.13+ 上运行：
  `pip install audioop-lts`，或改用 3.12。

---

## 3. 板子端接口

固件做了**数据面 / 控制面分离**，代理端必须按这个约定请求：

| 端口 | 角色 | 端点 |
| --- | --- | --- |
| 8080 | 数据面 | `/stream`、`/raw`、`/audio/alaw`、`/audio/down` |
| 8081 | 控制面 | `/health`、`/activity`、`/event`、`/notify`、`/cmd`、`/audio/alaw`、`/audio/down`、`/audio/level` |

**为什么要分端口**（这是实测踩出来的，不是设计洁癖）：

IDF 的 `http_server` 客户端 socket 槽位被硬限制在 `max_open_sockets(≤7) - 3 = 4` 个，
而 `/stream` 是长连接，一个客户端就长期占住 1 个 socket + 1 个 worker 线程。

故障现象：3 个浏览器 + 代理重连 → 板子 `/health` 连续超时，
看起来像"板子 HTTP 挂了"，但串口 `PROD` 日志一直显示 5.8fps 正常出图。

分离后实测（`test_stress.py`）：数据面被 3 路画面占满时，
控制面 10/10 次健康检查全部成功，延迟 86–260 ms，对讲和录音完全不受影响。

⚠️ 第二个 httpd 实例必须配**不同的 `ctrl_port`**（默认 32768），
否则 `bind` 失败 `errno 112`：`E httpd: error in creating ctrl socket (112)`。

---

## 4. 测试

```powershell
python test_console.py    # 端到端功能验收 (8 项)
python test_stress.py    # 数据面满载下的控制面可用性 (4 项)
```

`test_console.py` 覆盖：健康检查、活动检测、事件轮询、画面代理、
老人语音上行、对讲下行、事件回执、页面要素。

---

## 5. 已知限制

- **对讲是半双工**：按住说话时板子喇叭播放，同时麦克风上行会混入回声。
  固件侧有 `audio_board_set_downlink_active()` 做抑制，但效果需实地验证。
- **每个浏览器标签页会各拉一条控制台→板子的上游流**，
  靠 `SharedStream` 汇聚成**一条**共享连接，避免打爆板子 socket 槽位。
- 浏览器要求 `http://127.0.0.1` 或 `localhost` 才允许麦克风权限。
  若用局域网 IP 访问，需在 Chrome 里为该站点授权麦克风。
