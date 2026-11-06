#!/usr/bin/env python
"""
从 Cursor 的 agent-transcripts 里导出完整聊天记录为 Markdown。

背景：Cursor 的聊天记录曾出现"看不见"的情况。原始数据其实一直躺在
      ~/.cursor/projects/<workspace>/agent-transcripts/<uuid>/<uuid>.jsonl
      里，一行一条 JSON。本脚本把它还原成可读的 Markdown 归档。

用法：
    python tools/export_chat_history.py                # 导出全部会话
    python tools/export_chat_history.py <uuid>        # 只导出指定会话
    python tools/export_chat_history.py --grep 关键字 # 按内容筛选
    python tools/export_chat_history.py --list        # 只列出会话清单
"""
import json, os, re, glob, argparse, datetime

TRANSCRIPT_ROOT = os.path.join(os.path.expanduser("~"), ".cursor", "projects")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "docs", "chats")

# 会话标题：优先从 state.vscdb 的 composerHeaders 里取（更准确）
HEADER_DB = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Cursor",
                         "User", "globalStorage", "state.vscdb")


def load_titles():
    titles = {}
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{HEADER_DB}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT composerId, value FROM composerHeaders")
        for cid, val in cur.fetchall():
            try:
                titles[cid] = json.loads(val).get("name", "")
            except Exception:
                pass
        con.close()
    except Exception as e:
        print(f"[警告] 读取标题库失败: {e}")
    return titles


def flatten(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                t = c.get("type")
                if t == "text":
                    parts.append(c.get("text", ""))
                elif t == "tool_use":
                    inp = c.get("input", {}) or {}
                    desc = (inp.get("command") or inp.get("file_path")
                            or inp.get("pattern") or inp.get("description") or "")
                    parts.append(f"**[工具调用 {c.get('name')}]** `{str(desc)[:400]}`")
                elif t == "tool_result":
                    body = c.get("content")
                    s = body if isinstance(body, str) else json.dumps(
                        body, ensure_ascii=False, default=str)
                    parts.append(f"**[工具结果]**\n```\n{str(s)[:2000]}\n```")
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return flatten(content.get("content"))
    return str(content)


def clean(text):
    """去掉环境详情等噪音块。"""
    text = re.sub(r"<environment_details>.*?</environment_details>",
                  "<!-- 环境详情已省略 -->", text, flags=re.S)
    text = re.sub(r"<available_subagent_types>.*?</available_subagent_types>",
                  "<!-- 子代理类型列表已省略 -->", text, flags=re.S)
    return text.strip()


def parse_transcript(path):
    """返回 [(role, ts, text), ...]"""
    msgs = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            role = o.get("role")
            if role not in ("user", "assistant"):
                continue
            text = clean(flatten(o.get("message", {}).get("content")))
            if not text:
                continue
            ts = o.get("timestamp") or o.get("createdAt") or ""
            msgs.append((role, ts, text))
    return msgs


def fmt_ts(ts):
    if not ts:
        return "?"
    try:
        if isinstance(ts, str):
            return ts.replace("T", " ")[:19]
        ts = int(ts)
        if ts > 1e12:
            ts //= 1000
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)[:19]


def all_transcripts():
    out = []
    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "*", "agent-transcripts",
                                      "*", "*.jsonl")):
        out.append(path)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uuid", nargs="?", default=None)
    ap.add_argument("--grep", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    titles = load_titles()
    files = all_transcripts()

    # 按会话 id 过滤
    if args.uuid:
        files = [p for p in files if args.uuid in os.path.basename(p)]

    results = []
    for p in files:
        cid = os.path.splitext(os.path.basename(p))[0]
        msgs = parse_transcript(p)
        if not msgs:
            continue
        title = titles.get(cid, "")
        results.append((msgs[-1][1], cid, title, msgs, p))

    results.sort(key=lambda r: r[0], reverse=True)

    if args.grep:
        g = args.grep.lower()
        results = [r for r in results
                   if g in (r[2] or "").lower()
                   or g in "\n".join(m[2] for m in r[3]).lower()]

    print(f"共 {len(results)} 个会话匹配\n")
    if args.list:
        for ts, cid, title, msgs, p in results:
            print(f"{fmt_ts(ts)}  {len(msgs):>5} 条  {cid}  {title[:60]}")
        return

    os.makedirs(args.out, exist_ok=True)
    for ts, cid, title, msgs, p in results:
        name = (title or cid).strip()
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)[:60].strip() or cid
        safe = safe.rstrip(".") or cid
        fn = os.path.join(args.out, f"{safe}__{cid[:8]}.md")
        with open(fn, "w", encoding="utf-8") as f:
            f.write(f"# {name}\n\n")
            f.write(f"- 会话 ID: `{cid}`\n")
            f.write(f"- 最后更新: {fmt_ts(ts)}\n")
            f.write(f"- 消息条数: {len(msgs)}\n")
            f.write(f"- 原始文件: `{p}`\n\n---\n\n")
            for role, t, text in msgs:
                r = "👤 用户" if role == "user" else "🤖 助手"
                f.write(f"## {r} · {fmt_ts(t)}\n\n{text}\n\n---\n\n")
        print(f"[导出] {len(msgs):>5} 条  {fn}")


if __name__ == "__main__":
    main()
