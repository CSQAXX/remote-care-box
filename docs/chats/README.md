# 聊天记录归档索引

> 归档时间：2026-11-06
> 归档原因：Cursor 侧边栏一度显示不出历史会话，本目录把原始记录固化到项目里，避免再次丢失。

## 原始数据位置

Cursor 把聊天记录存在两处，**都没丢**：

| 位置 | 内容 |
| --- | --- |
| `~/.cursor/projects/<workspace>/agent-transcripts/<uuid>/<uuid>.jsonl` | 逐行 JSON 的完整消息体（本归档的数据源） |
| `~/AppData/Roaming/Cursor/User/globalStorage/state.vscdb` | SQLite，表 `composerHeaders`（222 条会话标题与时间）、表 `cursorDiskKV`（`composerData:<uuid>` 存会话元数据） |

## 本目录内容（养老看护盒子相关）

| 文件 | 会话 | 条数 | 说明 |
| --- | --- | --- | --- |
| `一个装在老人家里的“远程看护盒子”，配一个子女手机上的小程序。__66d95941.md` | 2026-10-26 起 | 1610 | **项目起点**：需求文档 v1.0、设备调试、MJPEG/花屏/按键全链路 |
| `等下，你有没有仔细看我的需求文档…你给我规划下__353d6095.md` | 2026-11-05 | 444 | 上一轮规划 + 屏幕无画面问题排查（未完成） |
| `我现在要设计一款多功能的养老机器人…__77af7873.md` | 2026-11-04 | 316 | 养老机器人 PRD（`elder-care-robot` 目录产物） |
| `你给我计划下，我现在有大约35个学生…__c9a059ac.md` | 2026-11-02 | 391 | 35 个学生分组做项目的组织方案 |
| `你给我计划下，我现在有大约35个学生…__d08002d1.md` | 2026-11-01 | 1 | 上一条的续接 |
| `C_Users_陈思晴_cursor-pilot_…__5db1a1fc.md` | 2026-10-31 | 1926 | Cursor 自动运行/本地模型额度（非本项目，误匹配归档） |

## 重新导出

```powershell
cd D:\Software\kanhu\remote-care-box

# 导出全部会话
python tools\export_chat_history.py

# 只导出含关键字的会话
python tools\export_chat_history.py --grep "看护"

# 只列清单，不落盘
python tools\export_chat_history.py --list
```

输出目录默认 `docs/chats/`。
