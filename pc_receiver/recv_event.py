#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电脑端事件接收端 —— 监听板子主动推来的 TCP 事件流
=================================================

对应需求：可行性验证需求文档 M2.6「板子主动 TCP 推送到电脑 :5555」

架构（推模式 / push）：
    ESP32-S3 BOX --TCP 连过来--> 本脚本 (:5556)
    本脚本 --POST /notify--> 板子确认收到

与拉模式（pull）的关系：
    拉模式 = 浏览器 JS 每 500ms 问板子 "GET /event"，取走就消失
    推模式 = 板子有事件就主动连过来推，一条 NDJSON 一行

两种模式可以同时开着，互不抢事件（事件带 seq，网页用 ?since= 去重）。

数据格式（NDJSON，每行一个 JSON）：
    {"type":"button","msg":"老人按了求助按钮","seq":12345,"ts":1700000000000}

用法：
    # 最简（板子 IP 默认走 /health 探测或手填）
    python recv_event.py --device 192.168.2.138

    # 指定端口
    python recv_event.py --device 192.168.2.138 --port 5556

    # 只打印不回执
    python recv_event.py --device 192.168.2.138 --no-notify

    # 后台跑
    python recv_event.py --device 192.168.2.138 > recv.log 2>&1
"""

import argparse
import json
import os
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

# 默认板子 IP —— DHCP 每次可能变，跑之前先确认
DEFAULT_DEVICE = "192.168.2.138"
DEFAULT_PORT = 5556
DEVICE_HTTP_PORT = 8080

# 存 JPEG 的目录 (相对本脚本)
DEFAULT_SAVE_DIR = "captures"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    # Windows 控制台默认 GBK, 遇到非法字符会直接抛 UnicodeEncodeError
    # 把不可编码的字符替换掉, 而不是让整个接收端崩掉。
    try:
        print(f"[{ts}] {msg}", flush=True)
    except UnicodeEncodeError:
        safe = msg.encode("gbk", errors="replace").decode("gbk")
        print(f"[{ts}] {safe}", flush=True)


class EventHandler(socketserver.StreamRequestHandler):
    """板子连上来后, 按行读取 NDJSON + 二进制 JPEG 帧。

    线路格式 (固件 push_client.c 定义):
        事件:  一行 JSON, 以 \\n 结尾
        JPEG:  一行 JSON 头 + 原始字节 + \\n
                {"type":"jpeg","seq":9,"len":12345,"ts":...}\\n<12345 字节>\\n

    为什么不能用 readline() 一把梭:
        JPEG 的【原始字节】里可能有 0x0A (换行符)! 二进制 JPEG 头里
        就有可能含 0x0A。按行读会把一帧 JPEG 切成好几段, 解析全乱。

    所以这里必须:
        1. 先读到第一个 \\n 结束的行 -> 解析成 JSON
        2. 如果是 jpeg 头, 再精确读 len 字节 + 1 字节换行
    """

    # 板子可能长时间不发数据, 别让 socket 超时把连接掐断
    timeout = None

    def _read_until_nl(self) -> bytes:
        """一直读到 \\n 为止, 返回不含 \\n 的内容。

        用 readline() 的底层 rfile 但限制单行长度, 防止板子发了个
        巨大的东西把内存吃光。
        """
        chunks = []
        total = 0
        while True:
            ch = self.rfile.read(1)
            if not ch:
                raise ConnectionError("对端关闭")
            if ch == b"\n":
                break
            chunks.append(ch)
            total += 1
            if total > 8192:
                raise ValueError("单行过长, 协议异常")
        return b"".join(chunks)

    def _read_exact(self, n: int) -> bytes:
        """精确读 n 字节。"""
        data = self.rfile.read(n)
        if data is None or len(data) < n:
            raise ConnectionError(f"读取不足: 期望 {n}, 实得 {len(data) if data else 0}")
        return data

    def handle(self) -> None:
        peer = self.client_address
        log(f"板子已连接: {peer[0]}:{peer[1]}")

        try:
            while True:
                raw = self._read_until_nl()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                # 先看看是不是 JPEG 帧头
                jpeg_len = self._maybe_jpeg_header(line)
                if jpeg_len is not None:
                    self._handle_jpeg(jpeg_len)
                    continue

                self.handle_line(line)
        except (ConnectionResetError, BrokenPipeError):
            log("连接被重置")
        except ConnectionError as e:
            log(f"连接结束: {e}")
        except OSError as e:
            log(f"连接异常: {e}")
        finally:
            log(f"板子断开: {peer[0]}:{peer[1]}")

    def _maybe_jpeg_header(self, line: str):
        """判断这行是不是 JPEG 帧头, 是就返回长度, 不是返回 None。"""
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return None
        if ev.get("type") != "jpeg":
            return None
        try:
            return int(ev.get("len", 0))
        except (TypeError, ValueError):
            return 0

    def _handle_jpeg(self, length: int) -> None:
        """读掉一帧 JPEG 的原始字节, 存盘。"""
        data = self._read_exact(length)
        # 读掉结尾那个换行
        self._read_exact(1)

        if self.server.save_dir:
            name = f"jpeg_{int(time.time() * 1000)}_{length}.jpg"
            path = os.path.join(self.server.save_dir, name)
            try:
                with open(path, "wb") as f:
                    f.write(data)
                log(f"[JPEG] {length} 字节 -> {path}")
            except OSError as e:
                log(f"[JPEG] 存盘失败: {e}")
        else:
            log(f"[JPEG] {length} 字节")

    def handle_line(self, line: str) -> None:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            log(f"[非 JSON] {line[:200]}")
            return

        msg = ev.get("msg", "")
        seq = ev.get("seq", "-")
        ts = ev.get("ts", "-")
        etype = ev.get("type", "?")

        # §3.9 补传标记: 断网期间产生、恢复后补传的事件
        # 云端规则引擎据此【不重复告警】, 但仍然入库。
        replay = ev.get("replay", False)

        if etype == "heartbeat":
            log(f"[心跳] rssi={ev.get('rssi')} 堆={ev.get('free_heap')} "
                f"PSRAM={ev.get('free_psram')} 摄像头={ev.get('camera_ok')} "
                f"模式={ev.get('mode')} 对时={ev.get('time_synced')}")
            # 心跳不发给板子回执 —— 回执是给"人产生的事件"用的,
            # 心跳每 30s 一条, 回执只会刷屏。
            return

        if etype == "error":
            log(f"[错误] {ev.get('code')}: {ev.get('detail')}")
            if self.server.notify_enabled:
                self.server.notify(f"[错误] {ev.get('code')}", seq)
            return

        if replay:
            log(f"[收到·补传] type={etype} msg={msg!r} seq={seq} ts={ts}")
        else:
            log(f"[收到] type={etype} msg={msg!r} seq={seq} ts={ts}")

        if self.server.notify_enabled:
            self.server.notify(msg, seq)


class EventServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, device, notify_enabled, save_dir=None):
        super().__init__(addr, EventHandler)
        self.device = device
        self.notify_enabled = notify_enabled
        self.save_dir = save_dir
        self._lock = threading.Lock()
        self._seq_seen = set()

    def notify(self, msg: str, seq) -> None:
        """POST /notify 告诉板子"收到了"。

        板子那边会打日志 "电脑已确认收到: xxx" —— 这是验证闭环的关键证据。

        seq 去重: 同一个 seq 只回执一次, 防止重连后重复补传导致刷屏。
        """
        if seq != "-" and seq in self._seq_seen:
            return
        if seq != "-":
            with self._lock:
                self._seq_seen.add(seq)
                # 只保留最近 200 个 seq, 防止无限增长
                if len(self._seq_seen) > 200:
                    self._seq_seen = set(list(self._seq_seen)[-100:])

        url = f"http://{self.device}:{DEVICE_HTTP_PORT}/notify"
        body = (msg or "").encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            log(f"已回执 -> {url}")
        except urllib.error.URLError as e:
            log(f"回执失败 ({url}): {e}  —— 板子 IP 可能变了")
        except Exception as e:
            log(f"回执异常: {e}")


def probe_device(host: str, timeout: float = 2.0):
    """探一下板子的 /health, 顺便拿 IP 和 uptime 做启动自检。"""
    url = f"http://{host}:{DEVICE_HTTP_PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="远程看护盒子 —— 电脑端事件接收端 (TCP 推模式)")
    ap.add_argument("--device", default=DEFAULT_DEVICE,
                    help=f"板子 IP (默认 {DEFAULT_DEVICE})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"监听端口 (默认 {DEFAULT_PORT})")
    ap.add_argument("--no-notify", action="store_true",
                    help="只打印事件, 不 POST /notify 回执")
    ap.add_argument("--no-save", action="store_true",
                    help="不保存 JPEG (默认存到 captures/ 目录)")
    ap.add_argument("--save-dir", default=None,
                    help=f"JPEG 保存目录 (默认 {DEFAULT_SAVE_DIR})")
    ap.add_argument("--probe", action="store_true",
                    help="只探一次板子 /health 就退出")
    args = ap.parse_args()

    log("=" * 58)
    log("远程看护盒子 · 电脑端事件接收端")
    log(f"监听 0.0.0.0:{args.port}   板子 {args.device}:{DEVICE_HTTP_PORT}")
    log("=" * 58)

    health = probe_device(args.device)
    if health:
        log(f"板子在线: IP={health.get('ip')} uptime={health.get('uptime')}s "
            f"psram={health.get('psram')} audio={health.get('audio_ready')}")
    else:
        log(f"警告: 探不到板子 {args.device}:{DEVICE_HTTP_PORT} 的 /health")
        log("      (板子还没上电? Wi-Fi 没连? IP 变了? 板子日志里有当前 IP)")
        log("      事件本身仍然会收, 只是回执会失败。")

    if args.probe:
        return 0

    # JPEG 保存目录
    save_dir = None
    if not args.no_save:
        save_dir = args.save_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 DEFAULT_SAVE_DIR)
        try:
            os.makedirs(save_dir, exist_ok=True)
            log(f"JPEG 保存目录: {save_dir}")
        except OSError as e:
            log(f"创建保存目录失败 ({e}), 改为不保存 JPEG")
            save_dir = None

    # 端口被占用的话, 这里会抛 OSError
    try:
        server = EventServer(("0.0.0.0", args.port), args.device,
                             not args.no_notify, save_dir)
    except OSError as e:
        log(f"监听 {args.port} 失败: {e}")
        if "address already in use" in str(e).lower() or "10048" in str(e):
            log("端口被占。换一个:  --port 5557")
            log("同时要改固件 firmware/main/camera_pins.h 的 PC_RECEIVER_PORT")
        return 1

    log(f"就绪。等待板子连接... (Ctrl+C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("\n退出。")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
