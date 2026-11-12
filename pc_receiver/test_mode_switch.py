#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「⑤ 模式切换」离线验收：不烧板子、不联网、不改动任何固件。

全部走 dry-run（MODE_SWITCH_DRY_RUN=1），只验证：
  模块加载 / 镜像齐备 / 状态接口 / 两条烧录命令 / 页面元素 / WSGI 路由。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MODE_SWITCH_DRY_RUN"] = "1"

import mode_switch
import pc_console

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name +
          (("  -> " + str(extra)) if extra and not cond else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="mode_switch_test_")
    mode_switch.STATE_FILE = os.path.join(tmp, "mode_state.json")
    mode_switch.LOG_FILE = os.path.join(tmp, "mode_switch.log")

    print("=== 1. 语法与模块 ===")
    import py_compile
    for f in ("pc_console.py", "mode_switch.py", "test_mode_switch.py"):
        try:
            py_compile.compile(os.path.join(HERE, f), doraise=True)
            check("编译 " + f, True)
        except Exception as e:
            check("编译 " + f, False, e)

    ms = mode_switch.ModeSwitcher("127.0.0.1")
    check("就绪检查无缺件", ms.problems() == [], ms.problems())
    check("esptool Python 存在", os.path.isfile(mode_switch.ESPTOOL_PY))
    check("小智 NVS 备份存在", os.path.isfile(mode_switch.NVS_XIAOZHI))

    print("=== 2. 状态接口 ===")
    st = ms.status()
    for k in ("ready", "mode", "mode_name", "next", "last_target", "busy",
              "log", "last_result", "com"):
        check("status 含 " + k, k in st)
    check("模式判定合法", st["mode"] in ("kanhu", "xiaozhi"), st["mode"])
    check("next 与 mode 相反", st["next"] != st["mode"])

    print("=== 3. 烧录命令 ===")
    ck = ms._commands("kanhu")
    cx = ms._commands("xiaozhi")
    check("切看护 = 1 条命令", len(ck) == 1, len(ck))
    check("看护用整片备份", mode_switch.IMG_KANHU in ck[0][1])
    check("看护用 --flash_size keep",
          "keep" in ck[0][1] and "0x0" in ck[0][1])
    check("切小智 = 2 条命令（固件 + 写回配置）", len(cx) == 2, len(cx))
    check("小智固件写到 0x0", "0x0" in cx[0][1])
    check("NVS 写回 0x9000 保住 Wi-Fi/音量", "0x9000" in cx[1][1])
    check("写回的是已备份 NVS", mode_switch.NVS_XIAOZHI in cx[1][1])
    check("两条命令都指定 esp32s3",
          all("esp32s3" in c for _, c in ck + cx))

    # --- kanhu-ai (自研固件 + AI 对话 + 「晴老师」唤醒词) ---
    ca = ms._commands("kanhu-ai")
    check("切 kanhu-ai = 1 条命令", len(ca) == 1, len(ca))
    cargs = ca[0][1]
    check("kanhu-ai 写的是自研固件 app", "kanhu_box.bin" in " ".join(cargs))
    check("kanhu-ai 带 MultiNet7 模型",
          any("srmodels.bin" in str(p) for p, _o, _l in mode_switch.KANHU_AI_PARTS))
    check("kanhu-ai 模型写进 model 分区 0x320000",
          any(o == 0x320000 for _p, o, _l in mode_switch.KANHU_AI_PARTS))
    check("kanhu-ai 带分区表 0x8000",
          any(o == 0x8000 for _p, o, _l in mode_switch.KANHU_AI_PARTS))
    check("kanhu-ai 指定 esp32s3", "esp32s3" in cargs)
    check("kanhu-ai 三个目标名字齐全",
          set(mode_switch.NAMES) == {"kanhu", "xiaozhi", "kanhu-ai"},
          sorted(mode_switch.NAMES))

    # 轮转顺序: kanhu -> kanhu-ai -> xiaozhi -> kanhu
    for start, want in (("kanhu", "kanhu-ai"), ("kanhu-ai", "xiaozhi"),
                        ("xiaozhi", "kanhu")):
        ms.last_target = start
        check("轮转 %s -> %s" % (start, want),
              ms._next_target() == want, ms._next_target())

    print("=== 4. dry-run 走完整流程 ===")
    r = ms.switch()
    check("switch 受理", r.get("ok") is True, r)
    check("重复点击被拦下", ms.switch().get("ok") is False)
    for _ in range(60):
        if not ms.busy:
            break
        time.sleep(0.05)
    check("后台线程正常收尾", ms.busy is False)
    check("结果为成功", ms.last_ok is True, ms.last_result)
    check("日志有 dry-run 记录", any("dry-run" in x for x in ms.log))
    check("状态文件已落盘", os.path.isfile(mode_switch.STATE_FILE))
    with open(mode_switch.STATE_FILE, encoding="utf-8") as f:
        saved = json.load(f)
    check("状态文件记录目标与结果",
          "last_target" in saved and "last_ok" in saved, saved)
    first = saved.get("last_target")
    r2 = ms.switch()
    check("第二次切换方向相反",
          r2.get("ok") is True and r2.get("target") != first, (first, r2))
    for _ in range(60):
        if not ms.busy:
            break
        time.sleep(0.05)

    print("=== 5. 页面元素 ===")
    html = pc_console.INDEX_HTML
    for frag in ("⑤ 模式切换", 'id="btnMode"', 'id="modeNow"',
                 'id="modeState"', "/api/mode/status", "/api/mode/switch",
                 "pollMode", "切成小智 AI", "回到看护模式"):
        check("页面含 " + frag, frag in html)
    src = open(os.path.join(HERE, "pc_console.py"), encoding="utf-8").read()
    check("后端有 get_mode", "def get_mode(" in src)
    check("后端有 mode 路由", 'path.startswith("/api/mode/")' in src)
    check("原有④ AI 对话仍完整", "④ AI 对话" in html and "/api/chat/status" in html)

    print("=== 5b. ⑥ 板端 AI 对话卡片 ===")
    for frag in ("⑥ 板端 AI 对话", 'id="aiState"', 'id="tWake"',
                 'id="tWakeHits"', 'id="tWakeProb"', 'id="tAiConn"',
                 'id="tAiTurns"', 'id="aiUser"', 'id="aiReply"',
                 'id="aiWakeWord"', "updateAiCard"):
        check("页面含 " + frag, frag in html)
    check("卡片提到唤醒词「晴老师」", "晴老师" in html)

    print("=== 5c. problems() 不再 AttributeError ===")
    # 回归: 早期版本 problems() 引用了不存在的 self.next,
    # 一调就 AttributeError -> 模式切换页面整个 500。
    try:
        ps = ms.problems()
        check("problems() 可调用", True, ps)
    except AttributeError as e:
        check("problems() 可调用", False, e)
    check("就绪检查无缺件", ms.problems() == [], ms.problems())

    print("=== 6. WSGI 路由实调 ===")
    pc_console.BOARD = SimpleNamespace(device="127.0.0.1")
    captured = {}

    def sr(status, headers):
        captured["status"] = status

    env0 = {"PATH_INFO": "/", "REQUEST_METHOD": "GET",
            "QUERY_STRING": "", "wsgi.input": io.BytesIO(b"")}
    page = b"".join(pc_console.app(env0, sr))
    check("首页返回 200", captured.get("status", "").startswith("200"),
          captured.get("status"))
    check("首页含模式切换卡片", "⑤ 模式切换" in page.decode("utf-8"))

    env = {"PATH_INFO": "/api/mode/status", "REQUEST_METHOD": "GET",
           "QUERY_STRING": "", "wsgi.input": io.BytesIO(b"")}
    body = b"".join(pc_console.app(env, sr))
    d = json.loads(body.decode("utf-8"))
    check("status 返回 200", captured.get("status", "").startswith("200"),
          captured.get("status"))
    check("status 返回合法 JSON", "mode" in d and "ready" in d, d)

    env2 = {"PATH_INFO": "/api/mode/switch", "REQUEST_METHOD": "POST",
            "QUERY_STRING": "", "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(b"")}
    body2 = b"".join(pc_console.app(env2, sr))
    d2 = json.loads(body2.decode("utf-8"))
    check("switch 路由可调用", "ok" in d2, d2)
    for _ in range(60):
        if not pc_console.MODE.busy:
            break
        time.sleep(0.05)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n=== 结果：%d 通过 / %d 失败 ===" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    print("全部通过（全程 dry-run，板子一行没烧）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
