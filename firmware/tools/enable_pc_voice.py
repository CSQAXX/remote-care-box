#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enable_pc_voice.py —— 把「小电脑」这个叫法接进看护固件（可一键还原）

【它做什么】
  1. 先备份所有要碰的文件到 firmware/pc_voice_backup_<时间戳>/
  2. 生成 main/pc_voice_config.h（电脑局域网 IP + 8767 口令，自动读）
  3. 往 3 个已有文件里【只插入】，不改不删任何原有行：
       main/wake_word.h        加 3 个宏（名字 / 拼音 / 命令词 id=316）
       main/wake_word.c        允许 id 316；命中后单独走 pc_voice，不进 voice_mode
       main/CMakeLists.txt     末尾追加一段：编译 pc_voice.c + 定义 ENABLE_PC_VOICE
  4. 往 model/fst/commands_cn.txt 追加第 316 行 "316,xiao dian nao"

【它不做什么】
  - 不动 main.c、voice_mode.c/h、ai_client.c、audio_board.c，一行都不动
  - 不动老人看板、老人云端、小智固件
  - 不烧录、不重启板子、不碰 build/ 里的产物
  - 所有插入都包在 ENABLE_PC_VOICE 里；关掉开关 = 和今天早上一模一样

【用法】
  python tools/enable_pc_voice.py --dry-run    # 只看会改什么，不写文件
  python tools/enable_pc_voice.py              # 备份 + 打补丁
  python tools/enable_pc_voice.py --revert     # 从最近一次备份还原
  python tools/enable_pc_voice.py --host 192.168.2.135   # 电脑 IP 变了就指定

【打完补丁之后，重烧前必须做】
  idf_build.bat build_models      # 重新打包词表，否则板子还是 315 条词
  idf_build.bat build             # 编译（不 flash）
  确认没问题，再挑老人不用的时候 flash。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

FIRMWARE = Path(__file__).resolve().parent.parent
MAIN = FIRMWARE / "main"
# 真正的打包源：esp-sr 组件里的 multinet_model/fst/commands_cn.txt
# （movemodel.py 第 160 行 copytree 的就是这份；改 model/fst/ 那份不会被打包）
MODEL = (FIRMWARE / "managed_components" / "espressif__esp-sr" / "model"
         / "multinet_model" / "fst" / "commands_cn.txt")
LOCAL_DEV = Path("D:/Software/local-dev")
IOT_JSON = LOCAL_DEV / "state" / "iot.json"
MARK = "PC_VOICE_PATCH v1"
BOARD_IP = "192.168.2.138"     # 只用来探测本机在哪个网段，不会连它

BACKUP_FILES = [
    MAIN / "wake_word.h",
    MAIN / "wake_word.c",
    MAIN / "CMakeLists.txt",
    MODEL,
    MAIN / "pc_voice_config.h",
]

# ---------------------------------------------------------------- 插入内容
H_PATCH = """
/* {mark} (删掉这整段即可还原) —— 第三个叫法「小电脑」 */
#ifdef ENABLE_PC_VOICE
#define WAKE_WORD_PC_NAME        "小电脑"
#define WAKE_WORD_PC_PINYIN      "xiao dian nao"
#define WAKE_WORD_PC_CMD_ID      316   /* commands_cn.txt 第 316 行 */
#endif
""".format(mark=MARK)

C_INCLUDE = """
#ifdef ENABLE_PC_VOICE
#include "pc_voice.h"   /* {mark} */
#endif
""".format(mark=MARK)

C_FILTER_NEW = """                    if (id != WAKE_WORD_CMD_ID && id != WAKE_WORD_XIAOZHI_CMD_ID
#ifdef ENABLE_PC_VOICE      /* {mark} */
                        && id != WAKE_WORD_PC_CMD_ID
#endif
                    ) {{
                        continue;
                    }}""".format(mark=MARK)

C_BRANCH = """#ifdef ENABLE_PC_VOICE
                    /* {mark}: 「小电脑」单独一条路, 不进 voice_mode, 不改看护模式 */
                    if (id == WAKE_WORD_PC_CMD_ID) {{
                        s_last_prob = res->prob[i];
                        s_hits++;
                        ESP_LOGI(TAG, "命中「小电脑」#%" PRIu32 " (概率 %.3f) -> 交给我的电脑",
                                 s_hits, s_last_prob);
                        int64_t pc_start = cur_pos - (WAKE_WORD_PREROLL_MS * 16);
                        s_hit_pos = pc_start;
                        esp_err_t pr = pc_voice_start_turn_at(pc_start);
                        if (pr != ESP_OK) {{
                            ESP_LOGW(TAG, "交给电脑失败: %s", esp_err_to_name(pr));
                        }}
                        break;
                    }}
#endif
""".format(mark=MARK)

CMAKE_PATCH = """
# ---------------------------------------------------------------------------
# {mark} (删掉这整段即可还原) —— 「小电脑」语音遥控我自己的电脑
#
# 纯追加, 上面 idf_component_register(...) 一行都没动。
# 想关掉: idf.py -DENABLE_PC_VOICE=0 build  (或把下面 set 的 1 改成 0)
# ---------------------------------------------------------------------------
set(ENABLE_PC_VOICE 1 CACHE BOOL "「小电脑」语音遥控我的电脑")
if(ENABLE_PC_VOICE)
    target_sources(${{COMPONENT_TARGET}} PRIVATE "pc_voice.c")
    target_compile_definitions(${{COMPONENT_TARGET}} PRIVATE "ENABLE_PC_VOICE=1")
endif()
""".format(mark=MARK)


def read(p: Path) -> str:
    """字节级读，不用 read_text —— 它会把 CRLF 吃掉，写回后整个文件都变。"""
    return p.read_bytes().decode("utf-8")


def write(p: Path, s: str, newline: str = "\n") -> None:
    """按文件原本的换行风格写回，保证 diff 里只有新增行。"""
    s = s.replace("\r\n", "\n")
    if newline == "\r\n":
        s = s.replace("\n", "\r\n")
    p.write_bytes(s.encode("utf-8"))


def lan_ip() -> str:
    """取能到板子那张网卡的本机 IP（UDP connect 不真的发包）。"""
    for target in (BOARD_IP, "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.3)
            s.connect((target, 80))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                return ip
        except OSError:
            pass
    return "192.168.2.135"


def get_token() -> str:
    try:
        return str(json.loads(IOT_JSON.read_text(encoding="utf-8"))["token"])
    except Exception as exc:
        raise SystemExit("读不到 8767 口令 (%s): %s" % (IOT_JSON, exc))


def backup() -> Path:
    dest = FIRMWARE / ("pc_voice_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    dest.mkdir(parents=True, exist_ok=False)
    for src in BACKUP_FILES:
        if src.exists():
            rel = src.relative_to(FIRMWARE)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
    (dest / "_files.txt").write_text(
        "\n".join(str(p.relative_to(FIRMWARE)) for p in BACKUP_FILES if p.exists()),
        encoding="utf-8")
    return dest


def latest_backup() -> Path:
    dirs = sorted(FIRMWARE.glob("pc_voice_backup_*"))
    if not dirs:
        raise SystemExit("没找到备份目录，无法还原")
    return dirs[-1]


def revert() -> None:
    src = latest_backup()
    manifest = src / "_files.txt"
    names = manifest.read_text(encoding="utf-8").splitlines() if manifest.exists() else []
    for name in names:
        shutil.copy2(src / name, FIRMWARE / name)
        print("已还原:", name)
    cfg = MAIN / "pc_voice_config.h"
    if "pc_voice_config.h" not in names and cfg.exists():
        cfg.unlink()
        print("已删除:", cfg.relative_to(FIRMWARE))
    print("\n还原完成（备份仍在 %s）。pc_voice.c/h 是纯新增文件，留着不编译也不影响。" %
          src.name)


def patch_file(path: Path, transform, label: str, dry: bool) -> None:
    raw = read(path)
    newline = "\r\n" if "\r\n" in raw else "\n"
    old = raw.replace("\r\n", "\n")          # 归一后做匹配，写回时还原风格
    new, note = transform(old)
    if new == old:
        print("  [跳过] %-28s %s" % (label, note or "已经打过补丁 / 无需改动"))
        return
    print("  [修改] %-28s %s" % (label, note))
    if not dry:
        write(path, new, newline)


def h_transform(s: str):
    if MARK in s:
        return s, "已打过"
    m = re.search(r"^(#define\s+WAKE_WORD_XIAOZHI_CMD_ID\s+315)\s*$", s, re.M)
    if not m:
        raise SystemExit("wake_word.h 里找不到 XIAOZHI_CMD_ID 315 这一行，停止（文件可能被改过）")
    return s[:m.end()] + "\n" + H_PATCH + s[m.end():], "加 3 个宏"


def c_transform(s: str):
    if MARK in s:
        return s, "已打过"
    anchor = '#include "wake_word.h"\n'
    if anchor not in s:
        raise SystemExit("wake_word.c 里找不到 #include \"wake_word.h\"，停止")
    s = s.replace(anchor, anchor + C_INCLUDE, 1)

    old_filter = ("                    if (id != WAKE_WORD_CMD_ID && "
                  "id != WAKE_WORD_XIAOZHI_CMD_ID) {\n"
                  "                        continue;\n"
                  "                    }")
    if old_filter not in s:
        raise SystemExit("wake_word.c 里找不到唤醒词过滤条件，停止（文件可能被改过）")
    s = s.replace(old_filter, C_FILTER_NEW, 1)

    anchor2 = "                    bool to_xiaozhi = (id == WAKE_WORD_XIAOZHI_CMD_ID);"
    if anchor2 not in s:
        raise SystemExit("wake_word.c 里找不到 to_xiaozhi 分支，停止")
    s = s.replace(anchor2, C_BRANCH + anchor2, 1)
    return s, "加 #include / 允许 316 / 加分支"


def cmake_transform(s: str):
    if MARK in s:
        return s, "已打过"
    if "idf_component_register" not in s:
        raise SystemExit("CMakeLists.txt 不像组件配置，停止")
    return s.rstrip("\n") + "\n" + CMAKE_PATCH, "末尾追加编译开关"


def model_transform(s: str):
    lines = [x.strip() for x in s.splitlines() if x.strip()]
    if any(x.startswith("316,") for x in lines):
        return s, "316 已存在"
    if len(lines) != 315:
        raise SystemExit("命令词表不是 315 行（实际 %d 行），停止，怕改坏词表" % len(lines))
    return "\n".join(lines) + "\n316,xiao dian nao\n", "追加第 316 条「小电脑」"


def main() -> None:
    ap = argparse.ArgumentParser(description="把「小电脑」接进看护固件（可还原）")
    ap.add_argument("--dry-run", action="store_true", help="只打印会改什么")
    ap.add_argument("--revert", action="store_true", help="从最近一次备份还原")
    ap.add_argument("--host", default="", help="电脑局域网 IP，默认自动探测")
    ap.add_argument("--token", default="", help="8767 口令，默认从 local-dev/state/iot.json 读")
    args = ap.parse_args()

    if args.revert:
        revert()
        return

    for p in (MAIN / "pc_voice.c", MAIN / "pc_voice.h", MAIN / "wake_word.c",
              MAIN / "wake_word.h", MAIN / "CMakeLists.txt", MODEL):
        if not p.exists():
            raise SystemExit("缺文件: %s" % p)

    host = args.host or lan_ip()
    tok = args.token or get_token()
    print("电脑地址: %s:8767" % host)
    print("口令    : %s...%s (来自 state/iot.json)" % (tok[:4], tok[-4:]))
    print("模式    : %s\n" % ("只看不改 --dry-run" if args.dry_run else "备份 + 打补丁"))

    if not args.dry_run:
        b = backup()
        print("备份目录: %s" % b.name)
        cfg = ('/* %s: 由 enable_pc_voice.py 生成，含局域网口令，别提交到公开仓库 */\n'
               '#pragma once\n'
               '#define PC_VOICE_HOST  "%s"\n'
               '#define PC_VOICE_PORT  8767\n'
               '#define PC_VOICE_TOKEN "%s"\n' % (MARK, host, tok))
        write(MAIN / "pc_voice_config.h", cfg)
        print("  [生成] main/pc_voice_config.h\n")

    patch_file(MAIN / "wake_word.h", h_transform, "wake_word.h", args.dry_run)
    patch_file(MAIN / "wake_word.c", c_transform, "wake_word.c", args.dry_run)
    patch_file(MAIN / "CMakeLists.txt", cmake_transform, "main/CMakeLists.txt", args.dry_run)
    patch_file(MODEL, model_transform, "model/fst/commands_cn.txt", args.dry_run)

    print("\n" + ("（dry-run 没写任何文件）" if args.dry_run else
                  "完成。现有文件只多了几行插入，原有逻辑一行没删没改。"))
    print("\n重烧前必须依次跑（先重新打包词表，否则板子还是 315 条词）：")
    print("  idf_build.bat build_models")
    print("  idf_build.bat build")
    print("\n反悔：python tools/enable_pc_voice.py --revert")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code:
            print("停止：%s" % exc, file=sys.stderr)
        raise
